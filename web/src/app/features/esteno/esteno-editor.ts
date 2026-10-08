import {
  Component,
  Directive,
  ElementRef,
  HostListener,
  OnDestroy,
  OnInit,
  computed,
  input,
  output,
  signal,
  viewChildren,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { switchMap } from 'rxjs';
import { BloqueAbierto, Segmento } from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { EstenoService } from '../../core/services/esteno.service';
import { SesionesService } from '../../core/services/sesiones.service';
import { ToastService } from '../../core/services/toast.service';
import { Division, DivisorTurno } from '../../shared/divisor-turno/divisor-turno';
import { confirmar } from '../../shared/utils/dialogos';
import { hms } from '../../shared/utils/texto';

const LATIDO_MS = 30000;
const SALTO_SEG = 5;
const COMANDOS = ['bold', 'italic', 'underline', 'strikeThrough', 'justifyFull', 'justifyLeft',
  'justifyCenter', 'insertUnorderedList', 'insertOrderedList'] as const;

/**
 * contenteditable que recibe su HTML una sola vez (al crearse): si se
 * enlazara en cada cambio, el cursor saltaría al inicio mientras se escribe.
 */
@Directive({ selector: '[appEditorRico]', standalone: true })
export class EditorRico implements OnInit {
  readonly html = input('', { alias: 'appEditorRico' });

  constructor(private readonly el: ElementRef<HTMLDivElement>) {}

  ngOnInit(): void {
    this.el.nativeElement.innerHTML = this.html();
  }

  leer(): string {
    return this.el.nativeElement.innerHTML;
  }
}

/** Segmentos consecutivos del mismo orador, editados como un solo turno. */
interface TurnoEditable {
  ids: number[];
  orador: string;
  html: string;
  inicio_seg: number;
  inicio_hms: string | null;
}

function agruparTurnos(segs: Segmento[]): TurnoEditable[] {
  const turnos: TurnoEditable[] = [];
  for (const s of segs) {
    const ultimo = turnos[turnos.length - 1];
    const orador = (s.orador || '').trim();
    const texto = (s.texto || '').trim();
    if (ultimo && ultimo.orador.trim() === orador) {
      ultimo.ids.push(s.id);
      if (texto) ultimo.html = ultimo.html ? `${ultimo.html} ${texto}` : texto;
    } else {
      turnos.push({
        ids: [s.id],
        orador: s.orador || '',
        html: texto,
        inicio_seg: s.inicio_seg,
        inicio_hms: s.inicio_hms,
      });
    }
  }
  return turnos;
}

@Component({
  selector: 'app-esteno-editor',
  standalone: true,
  imports: [FormsModule, EditorRico, DivisorTurno],
  templateUrl: './esteno-editor.html',
  styleUrl: './esteno-editor.scss',
})
export class EstenoEditor implements OnInit, OnDestroy {
  readonly sesionId = input.required<number>();
  readonly abierto = input.required<BloqueAbierto>();
  readonly nombres = input<string[]>([]);
  readonly hayAudio = input(false);

  /** terminado=true si el bloque se marcó como terminado. */
  readonly salir = output<{ terminado: boolean }>();

  private readonly editores = viewChildren(EditorRico);

  protected readonly turnos = signal<TurnoEditable[]>([]);
  protected readonly segmentos = signal<Segmento[]>([]);
  protected readonly bloque = computed(() => this.abierto().bloque);
  protected readonly guardando = signal(false);
  protected readonly dividiendo = signal<number | null>(null);
  protected readonly activos = signal<Set<string>>(new Set());

  // Reproductor
  protected readonly reproduciendo = signal(false);
  protected readonly actual = signal(0);
  protected readonly duracion = computed(() => Math.max(0, this.bloque().fin_seg - this.bloque().inicio_seg));
  protected readonly posicion = computed(() =>
    this.duracion() ? Math.round((1000 * Math.max(0, this.actual() - this.bloque().inicio_seg)) / this.duracion()) : 0,
  );
  protected soloMiTramo = true;
  protected velocidad = 1;

  protected readonly hms = hms;
  protected readonly idLista = 'esteno-nombres';

  private readonly audio = new Audio();
  private latido: ReturnType<typeof setInterval> | null = null;
  private liberado = false;

  constructor(
    private readonly esteno: EstenoService,
    private readonly sesiones: SesionesService,
    private readonly auth: AuthService,
    private readonly toast: ToastService,
  ) {}

  ngOnInit(): void {
    this.cargar(this.abierto());
    this.montarAudio();
    this.latido = setInterval(
      () => this.esteno.latido(this.sesionId(), this.bloque().indice).subscribe({ error: () => {} }),
      LATIDO_MS,
    );
  }

  ngOnDestroy(): void {
    if (this.latido) clearInterval(this.latido);
    this.audio.pause();
    this.audio.removeAttribute('src');
    // Si se sale navegando a otra pantalla, el bloque no se queda tomado.
    if (!this.liberado) this.soltarAlSalir();
  }

  /** Cerrar la pestaña con un bloque abierto también lo libera. */
  @HostListener('window:beforeunload')
  protected alCerrarPestana(): void {
    if (!this.liberado) this.soltarAlSalir();
  }

  private soltarAlSalir(): void {
    this.liberado = true;
    void fetch(`/api/esteno/${this.sesionId()}/bloques/${this.bloque().indice}/soltar`, {
      method: 'POST',
      keepalive: true,
      headers: { Authorization: `Bearer ${this.auth.token ?? ''}` },
    }).catch(() => {});
  }

  private cargar(b: BloqueAbierto): void {
    this.segmentos.set(b.segmentos);
    this.turnos.set(agruparTurnos(b.segmentos));
    this.dividiendo.set(null);
  }

  // ---------- Audio ----------
  private montarAudio(): void {
    if (!this.hayAudio()) return;
    const a = this.audio;
    a.preload = 'metadata';
    a.src = this.auth.conToken(this.esteno.urlAudio(this.sesionId()));
    a.addEventListener('loadedmetadata', () => {
      a.currentTime = this.bloque().inicio_seg;
      this.actual.set(a.currentTime);
    });
    a.addEventListener('timeupdate', () => {
      if (this.soloMiTramo && a.currentTime >= this.bloque().fin_seg) {
        a.currentTime = this.bloque().inicio_seg;
      }
      this.actual.set(a.currentTime);
    });
    a.addEventListener('play', () => this.reproduciendo.set(true));
    a.addEventListener('pause', () => this.reproduciendo.set(false));
    a.addEventListener('ended', () => this.reproduciendo.set(false));
    a.addEventListener('error', () => {
      if (a.getAttribute('src')) this.toast.error('No se pudo cargar el audio de la sesión.');
    });
  }

  alternarAudio(): void {
    if (!this.hayAudio()) {
      this.toast.info('No hay audio vinculado a esta sesión.');
      return;
    }
    if (this.audio.paused) void this.audio.play();
    else this.audio.pause();
  }

  saltar(segundos: number): void {
    const b = this.bloque();
    this.audio.currentTime = Math.min(b.fin_seg, Math.max(b.inicio_seg, this.audio.currentTime + segundos));
  }

  irA(segundo: number, reproducir = false): void {
    if (!this.hayAudio()) return;
    this.audio.currentTime = segundo;
    this.actual.set(segundo);
    if (reproducir) void this.audio.play();
  }

  moverPista(valor: string): void {
    this.irA(this.bloque().inicio_seg + this.duracion() * (Number(valor) / 1000));
  }

  cambiarVelocidad(v: number): void {
    this.velocidad = v;
    this.audio.playbackRate = v;
  }

  /** Atajos pensados también para un pedal: F1 atrasar · F2 play/pausa · F3 adelantar. */
  @HostListener('document:keydown', ['$event'])
  protected atajos(e: KeyboardEvent): void {
    if (e.key === 'F1') {
      e.preventDefault();
      this.saltar(-SALTO_SEG);
    } else if (e.key === 'F2') {
      e.preventDefault();
      this.alternarAudio();
    } else if (e.key === 'F3') {
      e.preventDefault();
      this.saltar(SALTO_SEG);
    }
  }

  // ---------- Formato ----------
  formato(evento: MouseEvent, comando: string): void {
    evento.preventDefault(); // no perder el foco del editor
    document.execCommand(comando, false);
    this.actualizarActivos();
  }

  @HostListener('document:selectionchange')
  protected actualizarActivos(): void {
    const enEditor = (document.activeElement as HTMLElement | null)?.classList.contains('editor-rico');
    const activos = new Set<string>();
    if (enEditor) {
      for (const c of COMANDOS) if (document.queryCommandState(c)) activos.add(c);
    }
    this.activos.set(activos);
  }

  // ---------- Turnos ----------
  /** Copia al modelo lo escrito en cada editor (antes de reordenar o guardar). */
  private sincronizar(): void {
    const editores = this.editores();
    this.turnos.update((ts) => ts.map((t, i) => ({ ...t, html: editores[i]?.leer() ?? t.html })));
  }

  elegirNombre(i: number, nombre: string): void {
    if (!nombre) return;
    this.turnos.update((ts) => ts.map((t, k) => (k === i ? { ...t, orador: nombre } : t)));
  }

  cambiarOrador(i: number, orador: string): void {
    this.turnos.update((ts) => ts.map((t, k) => (k === i ? { ...t, orador } : t)));
  }

  /** Pega este turno con el siguiente (caso típico: un corte sin orador detectado). */
  unirConSiguiente(i: number): void {
    this.sincronizar();
    this.turnos.update((ts) => {
      const a = ts[i];
      const b = ts[i + 1];
      if (!b) return ts;
      const unido: TurnoEditable = {
        ...a,
        ids: [...a.ids, ...b.ids],
        orador: a.orador.trim() ? a.orador : b.orador,
        html: [a.html.trim(), b.html.trim()].filter(Boolean).join(' '),
      };
      return [...ts.slice(0, i), unido, ...ts.slice(i + 2)];
    });
    this.toast.info('Turnos unidos. No olvides guardar.');
  }

  /** Texto plano original (de la base) del turno: sobre él se calcula el corte. */
  textoOriginal(t: TurnoEditable): string {
    return t.ids
      .map((id) => this.segmentos().find((s) => s.id === id)?.texto || '')
      .join(' ');
  }

  abrirDivisor(i: number): void {
    const t = this.turnos()[i];
    if (t.ids.some((id) => (this.segmentos().find((s) => s.id === id)?.texto || '').includes('<'))) {
      this.toast.info('Este turno ya tiene formato guardado: no se puede dividir por frases de forma segura.');
      return;
    }
    this.dividiendo.set(i);
  }

  /**
   * Divide en el servidor. Antes guarda lo escrito en los DEMÁS turnos para no
   * perderlo; el turno dividido se recarga desde la base.
   */
  dividir(i: number, d: Division): void {
    this.sincronizar();
    const t = this.turnos()[i];
    const resto = this.payload(new Set(t.ids));
    const sid = this.sesionId();
    const indice = this.bloque().indice;
    this.guardando.set(true);
    this.esteno
      .guardar(sid, indice, resto, false)
      .pipe(
        switchMap(() => this.sesiones.dividir(sid, { ids: t.ids, ...d })),
        switchMap(() => this.esteno.tomar(sid, indice)),
      )
      .subscribe({
        next: (b) => {
          this.guardando.set(false);
          this.cargar(b);
          this.toast.success('Turno dividido.');
        },
        error: (e) => {
          this.guardando.set(false);
          this.toast.error(mensajeDeError(e, 'No se pudo dividir el turno.'));
        },
      });
  }

  /**
   * El orador se aplica a todos los segmentos del turno; el texto (ya unido y
   * corregido) va en el primero y los demás quedan vacíos. Ids y tiempos se
   * conservan, así el audio sigue alineado.
   */
  private payload(excluir = new Set<number>()): { id: number; orador: string; texto: string }[] {
    const salida: { id: number; orador: string; texto: string }[] = [];
    for (const t of this.turnos()) {
      if (t.ids.some((id) => excluir.has(id))) continue;
      t.ids.forEach((id, k) => salida.push({ id, orador: t.orador, texto: k === 0 ? t.html : '' }));
    }
    return salida;
  }

  guardar(terminar: boolean): void {
    this.sincronizar();
    this.guardando.set(true);
    this.esteno.guardar(this.sesionId(), this.bloque().indice, this.payload(), terminar).subscribe({
      next: () => {
        this.guardando.set(false);
        if (terminar) {
          this.liberado = true;
          this.toast.success(`Bloque ${this.bloque().indice + 1} terminado. ¡Gracias!`);
          this.salir.emit({ terminado: true });
        } else {
          this.toast.success('Avance guardado.');
        }
      },
      error: (e) => {
        this.guardando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo guardar.'));
      },
    });
  }

  async cerrar(): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Salir sin guardar?',
      texto: 'Se perderán los cambios que no hayas guardado y el bloque quedará libre para otro corrector.',
      confirmar: 'Salir',
      peligro: true,
    });
    if (!ok) return;
    this.liberado = true;
    this.esteno.soltar(this.sesionId(), this.bloque().indice).subscribe({
      next: () => this.salir.emit({ terminado: false }),
      error: () => this.salir.emit({ terminado: false }),
    });
  }
}

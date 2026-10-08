import {
  Component,
  OnDestroy,
  computed,
  effect,
  input,
  numberAttribute,
  signal,
  untracked,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';
import { DetalleSesion, Fila, ResultadoCambio } from '../../../core/models';
import { mensajeDeError } from '../../../core/interceptors/auth.interceptor';
import { SesionesService } from '../../../core/services/sesiones.service';
import { ToastService } from '../../../core/services/toast.service';
import { descargarBlob } from '../../../shared/descargar-blob';
import { Division, DivisorTurno } from '../../../shared/divisor-turno/divisor-turno';
import { confirmar } from '../../../shared/utils/dialogos';
import {
  enlaceVideo,
  fechaCorta,
  hms,
  htmlAPlano,
  markdownAHtml,
  normalizar,
} from '../../../shared/utils/texto';

const INTERVALO_VIVO_MS = 2500;
const RANGO_IA: Record<string, number> = { validado: 3, media: 2, descartado: 1 };

/** Filas consecutivas del mismo orador, mostradas como una sola intervención. */
interface Turno {
  indice: number;
  orador: string;
  inicio: number;
  fin: number;
  ids: number[];
  textos: string[];
  voz: string | null;
  vozSim: number;
  revIa: string | null;
  motivoIa: string;
}

interface OradorResumen {
  nombre: string;
  minutos: number;
  turnos: number;
}

type Panel =
  | { tipo: 'orador'; ancla: number }
  | { tipo: 'texto'; ancla: number }
  | { tipo: 'dividir'; ancla: number };

function agrupar(filas: Fila[]): Turno[] {
  const turnos: Turno[] = [];
  let t: Turno | null = null;
  for (const f of filas) {
    if (!t || t.orador !== f.orador) {
      t = {
        indice: turnos.length,
        orador: f.orador,
        inicio: f.inicio_seg,
        fin: f.fin_seg,
        ids: [],
        textos: [],
        voz: null,
        vozSim: 0,
        revIa: null,
        motivoIa: '',
      };
      turnos.push(t);
    }
    t.ids.push(f.id);
    t.textos.push(htmlAPlano(f.texto));
    t.fin = f.fin_seg;
    if (f.voz_orador && (f.voz_similitud ?? 0) > t.vozSim) {
      t.voz = f.voz_orador;
      t.vozSim = f.voz_similitud ?? 0;
    }
    if (f.revisado_ia && (RANGO_IA[f.revisado_ia] ?? 0) > (RANGO_IA[t.revIa ?? ''] ?? 0)) {
      t.revIa = f.revisado_ia;
      t.motivoIa = f.motivo_ia ?? '';
    }
  }
  return turnos;
}

@Component({
  selector: 'app-revision',
  standalone: true,
  imports: [DecimalPipe, FormsModule, RouterLink, DivisorTurno],
  templateUrl: './revision.html',
  styleUrl: './revision.scss',
})
export class Revision implements OnDestroy {
  /** :id de la ruta (withComponentInputBinding). */
  readonly id = input.required({ transform: numberAttribute });

  protected readonly cargando = signal(true);
  protected readonly detalle = signal<DetalleSesion | null>(null);
  protected readonly catalogo = signal<string[]>([]);
  protected readonly filtroOrador = signal<string | null>(null);
  protected readonly busqueda = signal('');
  protected readonly enVivo = signal(true);
  protected readonly panel = signal<Panel | null>(null);
  /** Acción larga en curso (IA, compactar, Word…), para deshabilitar botones. */
  protected readonly ocupado = signal<string | null>(null);
  protected readonly resumenCargando = signal<number | null>(null);
  protected readonly resumenesOcultos = signal<Set<number>>(new Set());

  /** Valores de los editores abiertos. */
  protected nuevoOrador = '';
  protected textosEditados: string[] = [];

  protected readonly turnos = computed(() => agrupar(this.detalle()?.filas ?? []));
  protected readonly resumenes = computed(() => this.detalle()?.resumenes ?? {});
  protected readonly secciones = computed(() => {
    const mapa = new Map<number, string>();
    for (const s of this.detalle()?.estructura?.secciones ?? []) mapa.set(s.ancla_id, s.titulo);
    return mapa;
  });

  protected readonly oradores = computed<OradorResumen[]>(() => {
    const acc = new Map<string, OradorResumen>();
    for (const t of this.turnos()) {
      const o = acc.get(t.orador) ?? { nombre: t.orador, minutos: 0, turnos: 0 };
      o.minutos += Math.max(0, t.fin - t.inicio) / 60;
      o.turnos += 1;
      acc.set(t.orador, o);
    }
    return [...acc.values()].sort((a, b) => {
      const da = this.esDesconocido(a.nombre) ? 1 : 0;
      const db = this.esDesconocido(b.nombre) ? 1 : 0;
      return db - da || b.minutos - a.minutos;
    });
  });

  protected readonly visibles = computed(() => {
    const q = normalizar(this.busqueda().trim());
    const filtro = this.filtroOrador();
    return this.turnos().filter(
      (t) =>
        (!filtro || t.orador === filtro) &&
        (!q || normalizar(t.textos.join(' ')).includes(q) || normalizar(t.orador).includes(q)),
    );
  });

  /** Sugerencias para el campo de orador: catálogo, Mesa y los ya usados. */
  protected readonly sugerencias = computed(() => {
    const set = new Set<string>(['Presidencia / Mesa Directiva']);
    this.catalogo().forEach((n) => set.add('Dip. ' + n));
    this.turnos().forEach((t) => set.add(t.orador));
    return [...set].sort((a, b) => a.localeCompare(b, 'es'));
  });

  protected readonly pendientes = computed(
    () => this.turnos().filter((t) => this.esDesconocido(t.orador)).length,
  );

  protected readonly hms = hms;
  protected readonly fecha = fechaCorta;

  private firma = '';
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly sesiones: SesionesService,
    private readonly http: HttpClient,
    private readonly toast: ToastService,
  ) {
    this.sesiones.catalogo().subscribe({ next: (c) => this.catalogo.set(c), error: () => {} });
    this.timer = setInterval(() => this.tic(), INTERVALO_VIVO_MS);

    // Al entrar o al cambiar de sesión (:id) se parte de cero.
    effect(() => {
      this.id();
      untracked(() => {
        this.cargando.set(true);
        this.detalle.set(null);
        this.filtroOrador.set(null);
        this.panel.set(null);
        this.recargar();
      });
    });
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  recargar(): void {
    this.sesiones.detalle(this.id()).subscribe({
      next: (d) => this.aplicar(d),
      error: (e) => {
        this.cargando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo cargar la sesión.'));
      },
    });
  }

  private aplicar(d: DetalleSesion): void {
    const ult = d.filas[d.filas.length - 1];
    this.firma = `${d.filas.length}:${ult ? ult.id + ':' + (ult.texto || '').length : 0}:${Object.keys(d.resumenes).length}:${d.estructura?.secciones.length ?? 0}`;
    this.detalle.set(d);
    this.cargando.set(false);
  }

  /** Auto-actualización: sigue la transcripción en vivo sin pisar lo que editas. */
  private tic(): void {
    if (!this.enVivo() || document.hidden || this.panel() || this.ocupado() || !this.detalle()) return;
    const doc = document.documentElement;
    const cercaDelFinal = window.innerHeight + window.scrollY >= doc.scrollHeight - 160;
    this.sesiones.detalle(this.id()).subscribe({
      next: (d) => {
        const ult = d.filas[d.filas.length - 1];
        const firma = `${d.filas.length}:${ult ? ult.id + ':' + (ult.texto || '').length : 0}:${Object.keys(d.resumenes).length}:${d.estructura?.secciones.length ?? 0}`;
        if (firma === this.firma || this.panel()) return;
        this.aplicar(d);
        if (cercaDelFinal) {
          setTimeout(() => window.scrollTo({ top: document.documentElement.scrollHeight }), 50);
        }
      },
      error: () => {},
    });
  }

  alternarVivo(): void {
    this.enVivo.update((v) => !v);
    this.toast.info(
      this.enVivo()
        ? 'Auto-actualización activada: la transcripción se actualiza sola (se pausa mientras editas).'
        : 'Auto-actualización en pausa.',
    );
  }

  esDesconocido(nombre: string): boolean {
    return !nombre || normalizar(nombre) === 'desconocido';
  }

  filtrarOrador(nombre: string): void {
    this.filtroOrador.update((actual) => (actual === nombre ? null : nombre));
  }

  enlace(t: Turno): string | null {
    return enlaceVideo(this.detalle()?.sesion.url, t.inicio);
  }

  /** Texto del turno con la búsqueda resaltada (escapado antes de marcar). */
  textoResaltado(t: Turno): string {
    const d = document.createElement('div');
    d.textContent = t.textos.join(' ');
    let html = d.innerHTML;
    const q = this.busqueda().trim();
    if (q) {
      const rex = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      html = html.replace(rex, '<mark>$1</mark>');
    }
    return html;
  }

  resumenHtml(t: Turno): string {
    return markdownAHtml(this.resumenes()[t.ids[0]] ?? '');
  }

  tieneResumen(t: Turno): boolean {
    return !!this.resumenes()[t.ids[0]];
  }

  resumenVisible(t: Turno): boolean {
    return this.tieneResumen(t) && !this.resumenesOcultos().has(t.ids[0]);
  }

  panelDe(t: Turno): Panel['tipo'] | null {
    const p = this.panel();
    return p && p.ancla === t.ids[0] ? p.tipo : null;
  }

  // ---------- Orador ----------
  abrirOrador(t: Turno): void {
    this.nuevoOrador = t.orador;
    this.panel.set({ tipo: 'orador', ancla: t.ids[0] });
  }

  asignar(t: Turno, orador: string, mensaje = 'Orador actualizado'): void {
    const valor = orador.trim();
    if (!valor) return;
    this.ejecutar('orador', this.sesiones.cambiarOrador(this.id(), t.ids, valor), (r) =>
      `${mensaje}: ${r.cambios ?? 0} segmento(s)${r.unidos ? ` · ${r.unidos} unidos` : ''}.`,
    );
  }

  renombrarEnSesion(t: Turno): void {
    const valor = this.nuevoOrador.trim();
    if (!valor) return;
    this.ejecutar('orador', this.sesiones.renombrar(this.id(), t.orador, valor), (r) =>
      `«${t.orador}» → «${valor}» en toda la sesión: ${r.cambios ?? 0} cambio(s).`,
    );
  }

  anterior(t: Turno): Turno | undefined {
    return this.turnos()[t.indice - 1];
  }

  siguiente(t: Turno): Turno | undefined {
    return this.turnos()[t.indice + 1];
  }

  // ---------- Texto ----------
  abrirTexto(t: Turno): void {
    this.textosEditados = [...t.textos];
    this.panel.set({ tipo: 'texto', ancla: t.ids[0] });
  }

  guardarTexto(t: Turno): void {
    const cambios = t.ids
      .map((id, k) => ({ id, texto: this.textosEditados[k] ?? '' }))
      .filter((c, k) => c.texto !== t.textos[k]);
    if (!cambios.length) {
      this.panel.set(null);
      return;
    }
    this.ejecutar('texto', this.sesiones.guardarTextos(this.id(), cambios), (r) =>
      `Texto corregido: ${r.cambios ?? 0} segmento(s).`,
    );
  }

  // ---------- Dividir ----------
  abrirDividir(t: Turno): void {
    this.panel.set({ tipo: 'dividir', ancla: t.ids[0] });
  }

  dividir(t: Turno, d: Division): void {
    this.ocupado.set('dividir');
    this.sesiones.dividir(this.id(), { ids: t.ids, ...d }).subscribe({
      next: () => this.terminar('Turno dividido en dos oradores.'),
      error: (e) => this.fallar(e, 'No se pudo dividir.'),
    });
  }

  // ---------- Resumen ----------
  pedirResumen(t: Turno): void {
    if (this.tieneResumen(t)) {
      this.resumenesOcultos.update((s) => {
        const n = new Set(s);
        if (n.has(t.ids[0])) n.delete(t.ids[0]);
        else n.add(t.ids[0]);
        return n;
      });
      return;
    }
    this.resumenCargando.set(t.ids[0]);
    this.sesiones.pedirResumen(this.id(), t.orador, t.textos.join(' '), t.ids[0]).subscribe({
      next: async (r) => {
        this.resumenCargando.set(null);
        if (r.modo === 'auto' && r.resumen) {
          this.recargar();
        } else if (r.prompt) {
          await navigator.clipboard.writeText(r.prompt).catch(() => {});
          this.toast.info(
            r.error ??
              'Sin clave de IA configurada: se copió el prompt para pegarlo en Claude (claude.ai).',
          );
        }
      },
      error: (e) => {
        this.resumenCargando.set(null);
        this.toast.error(mensajeDeError(e, 'No se pudo generar el resumen.'));
      },
    });
  }

  copiarResumen(t: Turno): void {
    void navigator.clipboard.writeText(this.resumenes()[t.ids[0]] ?? '');
    this.toast.success('Resumen copiado al portapapeles.');
  }

  async borrarResumen(t: Turno): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Borrar el resumen?',
      texto: 'Se quitará de la sesión y del anexo del Word.',
      confirmar: 'Borrar',
      peligro: true,
    });
    if (!ok) return;
    this.ejecutar('resumen', this.sesiones.borrarResumen(this.id(), t.ids[0]), () => 'Resumen borrado.');
  }

  // ---------- Acciones de la sesión ----------
  compactar(): void {
    this.ejecutar('compactar', this.sesiones.compactar(this.id()), (r) =>
      r.unidos ? `${r.unidos} registro(s) unidos en la base.` : 'No había registros consecutivos por unir.',
    );
  }

  async corregirEstilo(): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Corregir el estilo con IA?',
      texto:
        'Se enviarán todos los segmentos a la IA para corregir ortografía, puntuación y párrafos. ' +
        'El texto original se conserva como respaldo. Puede tardar unos minutos.',
      confirmar: 'Corregir',
    });
    if (!ok) return;
    this.ejecutar('estilo', this.sesiones.corregirEstilo(this.id()), (r) =>
      `Revisión completada: ${r.cambios ?? 0} segmento(s) corregidos.${r.aviso ? ' ⚠ ' + r.aviso : ''}`,
    );
  }

  async estructurar(): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Numerar el orden del día?',
      texto:
        'La IA leerá la sesión para detectar dónde empieza cada punto del orden del día y ' +
        'numerarlos (1., 2., 3.…). No modifica el texto de las intervenciones.',
      confirmar: 'Numerar',
    });
    if (!ok) return;
    this.ejecutar('estructura', this.sesiones.estructurar(this.id()), (r) =>
      `Orden del día detectado: ${r.secciones ?? 0} punto(s). Ya aparecen aquí y en el Word.`,
    );
  }

  async borrarEstructura(): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Quitar la numeración?',
      texto: 'Se borrarán los encabezados del orden del día de esta sesión.',
      confirmar: 'Quitar',
      peligro: true,
    });
    if (!ok) return;
    this.ejecutar('estructura', this.sesiones.borrarEstructura(this.id()), () => 'Numeración quitada.');
  }

  descargarWord(): void {
    this.ocupado.set('word');
    descargarBlob(this.http, this.sesiones.urlWord(this.id()), `Sesion_${this.id()}.docx`).subscribe({
      next: () => this.ocupado.set(null),
      error: (e) => this.fallar(e, 'No se pudo generar el Word.'),
    });
  }

  // ---------- Ayudantes ----------
  private ejecutar(
    accion: string,
    peticion: Observable<ResultadoCambio>,
    mensaje: (r: ResultadoCambio) => string,
  ): void {
    this.ocupado.set(accion);
    peticion.subscribe({
      next: (r) => this.terminar(mensaje(r)),
      error: (e) => this.fallar(e, 'No se pudo completar la acción.'),
    });
  }

  private terminar(mensaje: string): void {
    this.ocupado.set(null);
    this.panel.set(null);
    this.toast.success(mensaje);
    this.recargar();
  }

  private fallar(e: unknown, porDefecto: string): void {
    this.ocupado.set(null);
    this.toast.error(mensajeDeError(e, porDefecto));
  }
}

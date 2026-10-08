import {
  Component,
  OnDestroy,
  Signal,
  computed,
  effect,
  input,
  numberAttribute,
  signal,
  untracked,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Bloque, BloqueAbierto, EstadoEsteno, Usuario } from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { EstenoService } from '../../core/services/esteno.service';
import { SesionesService } from '../../core/services/sesiones.service';
import { ToastService } from '../../core/services/toast.service';
import { UsuariosService } from '../../core/services/usuarios.service';
import { descargarBlob } from '../../shared/descargar-blob';
import { duracionLegible, hms } from '../../shared/utils/texto';
import { EstenoEditor } from './esteno-editor';

const INTERVALO_MS = 4000;
type Vista = 'plan' | 'bloques';

@Component({
  selector: 'app-esteno-sesion',
  standalone: true,
  imports: [FormsModule, NgTemplateOutlet, RouterLink, EstenoEditor],
  templateUrl: './esteno-sesion.html',
  styleUrl: './esteno-sesion.scss',
})
export class EstenoSesion implements OnDestroy {
  readonly id = input.required({ transform: numberAttribute });

  protected readonly esAdmin: Signal<boolean>;
  protected readonly yo: Signal<Usuario | null>;
  protected readonly estado = signal<EstadoEsteno | null>(null);
  protected readonly cargando = signal(true);
  protected readonly vista = signal<Vista>('bloques');
  protected readonly abierto = signal<BloqueAbierto | null>(null);
  protected readonly abriendo = signal<number | null>(null);
  protected readonly nombres = signal<string[]>([]);
  protected readonly usuarios = signal<Usuario[]>([]);
  protected readonly guardandoPlan = signal(false);
  protected readonly filtroCorrector = signal('');
  protected readonly descargando = signal<string | null>(null);
  /** Turno elegido a mano por un corrector que el admin no registró en el plan. */
  protected readonly slotManual = signal<number | null>(null);

  // Formulario del plan (no se pisa con el refresco automático)
  protected numCorrectores = 2;
  protected minutosBloque = 10;
  protected asignados: string[] = [];
  protected rutaAudio = '';

  protected readonly hms = hms;
  protected readonly duracion = duracionLegible;

  protected readonly config = computed(() => this.estado()?.config ?? null);
  protected readonly bloques = computed(() => this.estado()?.bloques ?? []);
  protected readonly progreso = computed(() => {
    const p = this.estado()?.progreso ?? { terminados: 0, total: 0 };
    return { ...p, pct: p.total ? Math.round((100 * p.terminados) / p.total) : 0 };
  });

  /** Mi turno: el que el admin me asignó por email, o el que elegí a mano. */
  protected readonly miSlot = computed(() => {
    const email = (this.yo()?.email ?? '').toLowerCase();
    const i = (this.config()?.nombres ?? []).findIndex((n) => (n || '').trim().toLowerCase() === email);
    return i >= 0 ? i : this.slotManual();
  });
  protected readonly misBloques = computed(() =>
    this.miSlot() === null ? [] : this.bloques().filter((b) => b.slot === this.miSlot()),
  );
  protected readonly otrosBloques = computed(() =>
    this.miSlot() === null ? this.bloques() : this.bloques().filter((b) => b.slot !== this.miSlot()),
  );

  /** Personas que tocaron la sesión: las del plan y cualquiera que ayudó en otro turno. */
  protected readonly personas = computed(() => {
    const set = new Set<string>();
    (this.config()?.nombres ?? []).forEach((n) => n && set.add(n));
    this.bloques().forEach((b) => {
      if (b.tomado_por) set.add(b.tomado_por);
      if (b.terminado_por) set.add(b.terminado_por);
    });
    return [...set].sort();
  });
  protected readonly bloquesFiltrados = computed(() => {
    const f = this.filtroCorrector();
    return f ? this.bloques().filter((b) => b.tomado_por === f || b.terminado_por === f) : this.bloques();
  });
  protected readonly enlace = computed(() => `${location.origin}/esteno/${this.id()}`);

  private timer: ReturnType<typeof setInterval> | null = null;
  private planCargado = false;

  constructor(
    private readonly esteno: EstenoService,
    private readonly sesiones: SesionesService,
    private readonly usuariosService: UsuariosService,
    private readonly http: HttpClient,
    private readonly toast: ToastService,
    auth: AuthService,
  ) {
    this.esAdmin = auth.esAdmin;
    this.yo = auth.currentUser;
    if (this.esAdmin()) {
      this.vista.set('plan');
      this.usuariosService.listar().subscribe({ next: (u) => this.usuarios.set(u), error: () => {} });
    }

    effect(() => {
      const id = this.id();
      untracked(() => {
        this.cargando.set(true);
        this.abierto.set(null);
        this.planCargado = false;
        this.slotManual.set(this.leerSlotManual(id));
        this.cargar();
        this.esteno.nombres(id).subscribe({ next: (r) => this.nombres.set(r.nombres), error: () => {} });
      });
    });

    this.timer = setInterval(() => this.tic(), INTERVALO_MS);
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  private tic(): void {
    if (document.hidden || this.abierto()) return;
    // No refrescar mientras se escribe/elige en un campo del formulario.
    const a = document.activeElement;
    if (a && a.matches('input, select, textarea')) return;
    this.cargar();
  }

  cargar(): void {
    this.esteno.estado(this.id()).subscribe({
      next: (e) => {
        this.estado.set(e);
        this.cargando.set(false);
        if (!this.planCargado) {
          this.planCargado = true;
          this.numCorrectores = e.config?.num_correctores ?? 2;
          this.minutosBloque = e.config ? e.config.bloque_seg / 60 : 10;
          this.asignados = [...(e.config?.nombres ?? [])];
        }
      },
      error: (e) => {
        this.cargando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo cargar el estado de la sesión.'));
      },
    });
  }

  nombreSlot(slot: number): string {
    const n = this.config()?.nombres?.[slot];
    return n && n.trim() ? n : `Corrector ${slot + 1}`;
  }

  turnos(): number[] {
    return Array.from({ length: Math.max(1, Math.min(50, this.numCorrectores || 1)) }, (_, i) => i);
  }

  // ---------- Admin ----------
  generarPlan(): void {
    this.guardandoPlan.set(true);
    const nombres = this.turnos().map((i) => this.asignados[i] ?? '');
    this.esteno
      .configurar(this.id(), {
        num_correctores: this.numCorrectores || 1,
        bloque_min: this.minutosBloque || 10,
        nombres,
      })
      .subscribe({
        next: (r) => {
          this.guardandoPlan.set(false);
          this.toast.success(`Plan generado: ${r.bloques} bloques${r.reinicio ? ' (estados reiniciados)' : ''}.`);
          this.cargar();
        },
        error: (e) => {
          this.guardandoPlan.set(false);
          this.toast.error(mensajeDeError(e, 'No se pudo generar el plan.'));
        },
      });
  }

  reasignar(b: Bloque, slot: number): void {
    this.esteno.reasignar(this.id(), b.indice, slot).subscribe({
      next: () => this.cargar(),
      error: (e) => this.toast.error(mensajeDeError(e)),
    });
  }

  vincularAudio(): void {
    this.esteno.vincularAudio(this.id(), this.rutaAudio.trim()).subscribe({
      next: (r) => {
        this.toast[r.existe ? 'success' : 'info'](
          r.existe ? 'Audio vinculado correctamente.' : 'Ruta guardada, pero el archivo no existe todavía.',
        );
        this.cargar();
      },
      error: (e) => this.toast.error(mensajeDeError(e)),
    });
  }

  copiarEnlace(): void {
    void navigator.clipboard.writeText(this.enlace());
    this.toast.success('Enlace copiado.');
  }

  descargarWord(tipo: 'corrector' | 'autores'): void {
    const corrector = this.filtroCorrector();
    const url =
      tipo === 'corrector'
        ? this.sesiones.urlWord(this.id(), { corrector })
        : this.sesiones.urlWord(this.id(), { marcarAutores: true });
    this.descargando.set(tipo);
    descargarBlob(this.http, url, `Sesion_${this.id()}.docx`).subscribe({
      next: () => this.descargando.set(null),
      error: (e) => {
        this.descargando.set(null);
        this.toast.error(mensajeDeError(e, 'No se pudo generar el Word.'));
      },
    });
  }

  // ---------- Corrector ----------
  elegirSlot(slot: number): void {
    this.slotManual.set(slot);
    try {
      localStorage.setItem(`esteno_slot_${this.id()}`, String(slot));
    } catch {
      /* sin almacenamiento local, solo dura esta visita */
    }
  }

  private leerSlotManual(id: number): number | null {
    try {
      const v = localStorage.getItem(`esteno_slot_${id}`);
      return v === null ? null : Number(v);
    } catch {
      return null;
    }
  }

  libre(b: Bloque): boolean {
    return b.estado !== 'editando' || b.vencido || b.tomado_por === this.yo()?.email;
  }

  abrir(b: Bloque): void {
    this.abriendo.set(b.indice);
    this.esteno.tomar(this.id(), b.indice).subscribe({
      next: (r) => {
        this.abriendo.set(null);
        this.abierto.set(r);
        window.scrollTo({ top: 0 });
      },
      error: (e) => {
        this.abriendo.set(null);
        this.toast.error(mensajeDeError(e, 'No se pudo abrir el bloque.'));
        this.cargar();
      },
    });
  }

  alSalirDelEditor(): void {
    this.abierto.set(null);
    this.cargar();
  }
}

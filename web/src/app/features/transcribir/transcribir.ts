import { Component, ElementRef, OnDestroy, computed, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { AgGridAngular } from 'ag-grid-angular';
import { ColDef, GetRowIdParams } from 'ag-grid-community';
import {
  Contextos,
  EstadoLanzador,
  EventoCongreso,
  LogTrabajo,
  Trabajo,
} from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { ToastService } from '../../core/services/toast.service';
import { TranscripcionService } from '../../core/services/transcripcion.service';
import { AG_GRID_LOCALE, gridTheme } from '../../shared/ag-grid/ag-grid-setup';
import { confirmar } from '../../shared/utils/dialogos';
import { fechaCorta } from '../../shared/utils/texto';
import {
  AccionesTrabajoParams,
  CeldaAccionesTrabajo,
  CeldaEstado,
  CeldaSesion,
} from './celdas-trabajo';

const INTERVALO_MS = 2500;
const INTERVALO_TRABAJOS_MS = 10000;

export const MODELOS = [
  { valor: 'tiny', texto: 'tiny — muy rápido' },
  { valor: 'base', texto: 'base — rápido' },
  { valor: 'small', texto: 'small — recomendado' },
  { valor: 'medium', texto: 'medium — más preciso' },
  { valor: 'large-v3', texto: 'large-v3 — lento, el más preciso' },
];

type Origen = 'evento' | 'url';

@Component({
  selector: 'app-transcribir',
  standalone: true,
  imports: [FormsModule, RouterLink, AgGridAngular],
  templateUrl: './transcribir.html',
  styleUrl: './transcribir.scss',
})
export class Transcribir implements OnDestroy {
  protected readonly modelos = MODELOS;
  protected readonly theme = gridTheme;
  protected readonly localeText = AG_GRID_LOCALE;

  protected readonly origen = signal<Origen>('evento');
  protected readonly enviando = signal(false);

  // --- Evento del Congreso ---
  protected readonly tipoEvento = signal(1);
  protected readonly eventos = signal<EventoCongreso[]>([]);
  protected readonly cargandoEventos = signal(false);
  protected readonly errorEventos = signal('');
  protected readonly eventoElegido = signal<string | null>(null);

  // --- URL manual ---
  protected readonly contextos = signal<Contextos | null>(null);
  protected url = '';
  protected tipo: 'pleno' | 'comision' = 'pleno';
  protected comisiones: string[] = [];
  protected fecha = '';
  protected modelo = 'small';
  protected readonly manual = signal<EstadoLanzador | null>(null);

  // --- Trabajos de la API ---
  protected readonly trabajos = signal<Trabajo[]>([]);
  protected readonly cargandoTrabajos = signal(true);
  protected readonly logAbierto = signal<LogTrabajo | null>(null);
  private readonly logManual = viewChild<ElementRef<HTMLPreElement>>('logManual');
  private readonly logTrabajo = viewChild<ElementRef<HTMLPreElement>>('logTrabajo');

  protected readonly activos = computed(() => this.trabajos().filter((t) => t.estado === 'ejecutando').length);

  protected readonly columnas: ColDef<Trabajo>[] = [
    {
      headerName: 'Evento / URL',
      field: 'url',
      flex: 2,
      minWidth: 240,
      tooltipField: 'url',
      cellClass: 'celda-url',
    },
    {
      headerName: 'Fuente',
      flex: 1,
      minWidth: 130,
      valueGetter: (p) => (p.data?.fuente === 'srt' ? `SRT · puerto ${p.data.puerto ?? '—'}` : 'YouTube'),
    },
    { headerName: 'Estado', field: 'estado', width: 140, cellRenderer: CeldaEstado },
    { headerName: 'Sesión', field: 'sesion_id', width: 100, cellRenderer: CeldaSesion },
    {
      headerName: 'Sin huella de voz',
      flex: 1,
      minWidth: 140,
      valueGetter: (p) => p.data?.participantes_no_encontrados.length ?? 0,
      tooltipValueGetter: (p) => p.data?.participantes_no_encontrados.join(', ') || 'Todos tienen huella',
    },
    {
      headerName: 'Creado',
      field: 'creado_en',
      width: 170,
      valueFormatter: (p) => fechaCorta(p.value),
      sort: 'desc',
    },
    {
      headerName: '',
      width: 190,
      sortable: false,
      cellRenderer: CeldaAccionesTrabajo,
      cellRendererParams: {
        onLog: (t: Trabajo) => this.verLog(t),
        onDetener: (t: Trabajo) => void this.detenerTrabajo(t),
        onReanudar: (t: Trabajo) => this.reanudarTrabajo(t),
      } satisfies Partial<AccionesTrabajoParams>,
    },
  ];
  protected readonly defaultColDef: ColDef = { sortable: true, resizable: true };
  protected readonly getRowId = (p: GetRowIdParams<Trabajo>) => p.data.id;

  private timer: ReturnType<typeof setInterval> | null = null;
  private timerTrabajos: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly transcripcion: TranscripcionService,
    private readonly toast: ToastService,
  ) {
    this.cargarEventos();
    this.transcripcion.contextos().subscribe({ next: (c) => this.contextos.set(c), error: () => {} });
    this.cargarTrabajos();
    this.consultarManual();
    this.timer = setInterval(() => this.tic(), INTERVALO_MS);
    this.timerTrabajos = setInterval(() => !document.hidden && this.cargarTrabajos(), INTERVALO_TRABAJOS_MS);
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
    if (this.timerTrabajos) clearInterval(this.timerTrabajos);
  }

  /** Refresca el log del lanzador manual y del trabajo abierto mientras corren. */
  private tic(): void {
    if (document.hidden) return;
    if (this.manual()?.corriendo) this.consultarManual();
    const abierto = this.logAbierto();
    if (abierto && (abierto.estado === 'ejecutando' || abierto.estado === 'deteniendo')) {
      this.transcripcion.log(abierto.id).subscribe({
        next: (l) => this.mostrarLog(l),
        error: () => {},
      });
    }
  }

  // ---------- Evento del Congreso ----------
  cambiarTipoEvento(tipo: number): void {
    this.tipoEvento.set(tipo);
    this.cargarEventos();
  }

  cargarEventos(): void {
    this.cargandoEventos.set(true);
    this.errorEventos.set('');
    this.eventoElegido.set(null);
    this.transcripcion.eventos(this.tipoEvento()).subscribe({
      next: (e) => {
        this.eventos.set(e);
        this.cargandoEventos.set(false);
      },
      error: (e) => {
        this.eventos.set([]);
        this.cargandoEventos.set(false);
        this.errorEventos.set(mensajeDeError(e, 'No se pudo consultar el sistema del Congreso.'));
      },
    });
  }

  crearDesdeEvento(): void {
    const id = this.eventoElegido();
    if (!id) return;
    this.enviando.set(true);
    this.transcripcion.crearDesdeEvento(id, this.tipoEvento(), this.modelo).subscribe({
      next: (t) => {
        this.enviando.set(false);
        let msg = `Trabajo creado (${t.fuente === 'srt' ? 'audio SRT' : 'YouTube'}).`;
        if (t.fuente === 'srt') msg += ` Esperando audio en el puerto ${t.puerto}.`;
        if (t.participantes_no_encontrados.length) {
          msg += ` Sin huella de voz: ${t.participantes_no_encontrados.join(', ')}.`;
        }
        this.toast.success(msg);
        this.cargarTrabajos();
        this.verLog(t);
      },
      error: (e) => {
        this.enviando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo crear el trabajo.'));
      },
    });
  }

  // ---------- URL manual ----------
  iniciarManual(): void {
    if (!this.url.trim().startsWith('http')) {
      this.toast.error('Pega una URL de video válida (http…).');
      return;
    }
    if (this.tipo === 'comision' && !this.comisiones.length) {
      this.toast.error('Elige al menos una comisión.');
      return;
    }
    this.enviando.set(true);
    this.transcripcion
      .lanzarManual({
        url: this.url.trim(),
        tipo: this.tipo,
        comisiones: this.tipo === 'comision' ? this.comisiones : [],
        fecha: this.tipo === 'pleno' ? this.fecha.trim() : '',
        modelo: this.modelo,
      })
      .subscribe({
        next: (r) => {
          this.enviando.set(false);
          this.toast.success('Transcripción iniciada. La sesión irá apareciendo en Sesiones.');
          if (r.aviso) this.toast.info(r.aviso);
          this.consultarManual();
        },
        error: (e) => {
          this.enviando.set(false);
          this.toast.error(mensajeDeError(e, 'No se pudo iniciar la transcripción.'));
        },
      });
  }

  async detenerManual(): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Detener la transcripción?',
      texto: 'Lo ya transcrito se conserva en la sesión.',
      confirmar: 'Detener',
      peligro: true,
    });
    if (!ok) return;
    this.transcripcion.detenerManual().subscribe({
      next: (r) => {
        this.toast.info(r.detenida ? 'Transcripción detenida.' : 'No había nada corriendo.');
        this.consultarManual();
      },
      error: (e) => this.toast.error(mensajeDeError(e)),
    });
  }

  private consultarManual(): void {
    this.transcripcion.estadoManual().subscribe({
      next: (est) => {
        this.manual.set(est);
        this.alFinal(this.logManual());
      },
      error: () => {},
    });
  }

  // ---------- Trabajos ----------
  cargarTrabajos(): void {
    this.transcripcion.trabajos().subscribe({
      next: (t) => {
        this.trabajos.set(t);
        this.cargandoTrabajos.set(false);
      },
      error: () => this.cargandoTrabajos.set(false),
    });
  }

  verLog(t: Trabajo): void {
    this.transcripcion.log(t.id).subscribe({
      next: (l) => {
        this.mostrarLog(l);
        setTimeout(() => this.logTrabajo()?.nativeElement.scrollIntoView({ behavior: 'smooth', block: 'center' }), 50);
      },
      error: (e) => this.toast.error(mensajeDeError(e, 'No se pudo leer el log.')),
    });
  }

  private mostrarLog(l: LogTrabajo): void {
    this.logAbierto.set(l);
    this.alFinal(this.logTrabajo());
  }

  async detenerTrabajo(t: Trabajo): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Detener este trabajo?',
      texto:
        'Desaparecerá de la lista del operador de audio. Lo transcrito se conserva y podrás reanudarlo después en la misma sesión.',
      confirmar: 'Detener',
      peligro: true,
    });
    if (!ok) return;
    this.transcripcion.detener(t.id).subscribe({
      next: () => {
        this.toast.info('Trabajo detenido.');
        this.cargarTrabajos();
      },
      error: (e) => this.toast.error(mensajeDeError(e)),
    });
  }

  reanudarTrabajo(t: Trabajo): void {
    this.transcripcion.reanudar(t.id).subscribe({
      next: () => {
        this.toast.success('Trabajo reanudado: ya debería aparecerle a la consola de audio.');
        this.cargarTrabajos();
      },
      error: (e) => this.toast.error(mensajeDeError(e)),
    });
  }

  /** Mantiene el log pegado al final si el usuario ya estaba abajo. */
  private alFinal(ref: ElementRef<HTMLPreElement> | undefined): void {
    const el = ref?.nativeElement;
    if (!el) return;
    const abajo = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    if (abajo || el.scrollTop === 0) setTimeout(() => (el.scrollTop = el.scrollHeight));
  }
}

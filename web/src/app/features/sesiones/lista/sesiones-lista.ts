import { Component, computed, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { Sesion } from '../../../core/models';
import { mensajeDeError } from '../../../core/interceptors/auth.interceptor';
import { SesionesService } from '../../../core/services/sesiones.service';
import { ToastService } from '../../../core/services/toast.service';
import { descargarBlob } from '../../../shared/descargar-blob';
import { diaLocal, duracionLegible, leerFecha, normalizar } from '../../../shared/utils/texto';

type Rango = 'todas' | 'hoy' | 'semana' | 'mes' | 'personalizado';

interface SesionVista extends Sesion {
  fecha: Date | null;
  dia: string; // AAAA-MM-DD local, '' si no tiene fecha
}

interface GrupoDia {
  dia: string;
  titulo: string;
  sesiones: SesionVista[];
}

function haceDias(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return diaLocal(d);
}

@Component({
  selector: 'app-sesiones-lista',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './sesiones-lista.html',
  styleUrl: './sesiones-lista.scss',
})
export class SesionesLista {
  protected readonly cargando = signal(true);
  protected readonly sesiones = signal<SesionVista[]>([]);
  protected readonly busqueda = signal('');
  protected readonly rango = signal<Rango>('todas');
  protected readonly desde = signal('');
  protected readonly hasta = signal('');
  protected readonly ocultarVacias = signal(false);
  protected readonly descargando = signal<number | null>(null);

  protected readonly rangos: { valor: Rango; texto: string }[] = [
    { valor: 'todas', texto: 'Todas' },
    { valor: 'hoy', texto: 'Hoy' },
    { valor: 'semana', texto: 'Últimos 7 días' },
    { valor: 'mes', texto: 'Últimos 30 días' },
  ];

  protected readonly vacias = computed(() => this.sesiones().filter((s) => !s.segmentos).length);

  protected readonly filtradas = computed(() => {
    const q = normalizar(this.busqueda().trim());
    const desde = this.desde();
    const hasta = this.hasta();
    const ocultar = this.ocultarVacias();
    return this.sesiones().filter(
      (s) =>
        (!ocultar || s.segmentos > 0) &&
        (!desde || (s.dia && s.dia >= desde)) &&
        (!hasta || (s.dia && s.dia <= hasta)) &&
        (!q || normalizar(`#${s.id} ${s.titulo ?? ''}`).includes(q)),
    );
  });

  /** Sesiones agrupadas por día (la más reciente primero). */
  protected readonly grupos = computed<GrupoDia[]>(() => {
    const grupos: GrupoDia[] = [];
    for (const s of this.filtradas()) {
      let g = grupos[grupos.length - 1];
      if (!g || g.dia !== s.dia) {
        g = { dia: s.dia, titulo: this.tituloDia(s.fecha), sesiones: [] };
        grupos.push(g);
      }
      g.sesiones.push(s);
    }
    return grupos;
  });

  protected readonly hayFiltros = computed(
    () => !!(this.busqueda() || this.desde() || this.hasta() || this.ocultarVacias()),
  );

  protected readonly duracion = duracionLegible;

  constructor(
    private readonly sesionesService: SesionesService,
    private readonly http: HttpClient,
    private readonly toast: ToastService,
  ) {
    this.cargar();
  }

  cargar(): void {
    this.cargando.set(true);
    this.sesionesService.listar().subscribe({
      next: (lista) => {
        this.sesiones.set(
          lista.map((s) => {
            const fecha = leerFecha(s.inicio);
            return { ...s, fecha, dia: fecha ? diaLocal(fecha) : '' };
          }),
        );
        this.cargando.set(false);
      },
      error: (e) => {
        this.cargando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudieron cargar las sesiones.'));
      },
    });
  }

  elegirRango(r: Rango): void {
    this.rango.set(r);
    const hoy = diaLocal(new Date());
    const inicio = { todas: '', hoy, semana: haceDias(6), mes: haceDias(29), personalizado: '' }[r];
    this.desde.set(inicio);
    this.hasta.set(r === 'todas' ? '' : hoy);
  }

  cambiarFecha(campo: 'desde' | 'hasta', valor: string): void {
    (campo === 'desde' ? this.desde : this.hasta).set(valor);
    this.rango.set('personalizado');
  }

  limpiar(): void {
    this.busqueda.set('');
    this.ocultarVacias.set(false);
    this.elegirRango('todas');
  }

  hora(s: SesionVista): string {
    return s.fecha
      ? s.fecha.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' })
      : '—';
  }

  private tituloDia(fecha: Date | null): string {
    if (!fecha) return 'Sin fecha';
    const dia = diaLocal(fecha);
    if (dia === diaLocal(new Date())) return 'Hoy';
    if (dia === haceDias(1)) return 'Ayer';
    const texto = fecha.toLocaleDateString('es-MX', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    });
    return texto.charAt(0).toUpperCase() + texto.slice(1);
  }

  descargarWord(s: Sesion): void {
    this.descargando.set(s.id);
    descargarBlob(this.http, this.sesionesService.urlWord(s.id), `Sesion_${s.id}.docx`).subscribe({
      next: () => this.descargando.set(null),
      error: (e) => {
        this.descargando.set(null);
        this.toast.error(mensajeDeError(e, 'No se pudo generar el Word.'));
      },
    });
  }

  esVideo(url: string | null): boolean {
    return !!url && url.startsWith('http');
  }
}

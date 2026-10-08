import { Component, computed, input, signal } from '@angular/core';
import { Sesion } from '../../core/models';
import { diaLocal, leerFecha, normalizar } from '../utils/texto';

export type Rango = 'todas' | 'hoy' | 'semana' | 'mes' | 'personalizado';

export interface SesionVista extends Sesion {
  fecha: Date | null;
  /** AAAA-MM-DD en hora local; '' si no tiene fecha. */
  dia: string;
}

export interface GrupoDia {
  dia: string;
  titulo: string;
  sesiones: SesionVista[];
}

function haceDias(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return diaLocal(d);
}

function tituloDia(fecha: Date | null): string {
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

/**
 * Estado de búsqueda de sesiones (texto, rango de fechas, ocultar vacías) y
 * el resultado agrupado por día. Lo comparten Sesiones y el módulo
 * estenográfico; la barra de filtros es <app-filtros-sesiones>.
 */
export class FiltroSesiones {
  readonly sesiones = signal<SesionVista[]>([]);
  readonly busqueda = signal('');
  readonly rango = signal<Rango>('todas');
  readonly desde = signal('');
  readonly hasta = signal('');
  readonly ocultarVacias = signal(false);

  readonly vacias = computed(() => this.sesiones().filter((s) => !s.segmentos).length);

  readonly filtradas = computed(() => {
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

  readonly grupos = computed<GrupoDia[]>(() => {
    const grupos: GrupoDia[] = [];
    for (const s of this.filtradas()) {
      let g = grupos[grupos.length - 1];
      if (!g || g.dia !== s.dia) {
        g = { dia: s.dia, titulo: tituloDia(s.fecha), sesiones: [] };
        grupos.push(g);
      }
      g.sesiones.push(s);
    }
    return grupos;
  });

  readonly hayFiltros = computed(
    () => !!(this.busqueda() || this.desde() || this.hasta() || this.ocultarVacias()),
  );

  cargar(lista: Sesion[]): void {
    this.sesiones.set(
      lista.map((s) => {
        const fecha = leerFecha(s.inicio);
        return { ...s, fecha, dia: fecha ? diaLocal(fecha) : '' };
      }),
    );
  }

  quitar(id: number): void {
    this.sesiones.update((lista) => lista.filter((s) => s.id !== id));
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
}

export function horaDe(s: SesionVista): string {
  return s.fecha ? s.fecha.toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' }) : '—';
}

/** Barra de filtros: buscador, Desde/Hasta, rangos rápidos y conteo. */
@Component({
  selector: 'app-filtros-sesiones',
  standalone: true,
  template: `
    @let f = filtro();
    <div class="tarjeta filtros">
      <div class="filtros__fila">
        <label class="buscador buscador--ancho">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.8" /><path d="m20 20-3.2-3.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>
          <input type="search" placeholder="Buscar por número, comisión o tema…" aria-label="Buscar sesión"
                 [value]="f.busqueda()" (input)="f.busqueda.set($any($event.target).value)" />
        </label>
        <label class="campo campo--fecha">
          <span class="campo__label">Desde</span>
          <input type="date" [value]="f.desde()" [max]="f.hasta() || null" (change)="f.cambiarFecha('desde', $any($event.target).value)" />
        </label>
        <label class="campo campo--fecha">
          <span class="campo__label">Hasta</span>
          <input type="date" [value]="f.hasta()" [min]="f.desde() || null" (change)="f.cambiarFecha('hasta', $any($event.target).value)" />
        </label>
      </div>
      <div class="filtros__fila filtros__fila--secundaria">
        <div class="rapidos" role="group" aria-label="Rango de fechas">
          @for (r of rangos; track r.valor) {
            <button type="button" class="rapido" [class.rapido--activo]="f.rango() === r.valor" (click)="f.elegirRango(r.valor)">{{ r.texto }}</button>
          }
        </div>
        @if (f.vacias()) {
          <label class="casilla">
            <input type="checkbox" [checked]="f.ocultarVacias()" (change)="f.ocultarVacias.set($any($event.target).checked)" />
            Ocultar sesiones sin texto ({{ f.vacias() }})
          </label>
        }
        <span class="filtros__total">
          <strong>{{ f.filtradas().length }}</strong> de {{ f.sesiones().length }} sesiones
          @if (f.hayFiltros()) {
            · <button type="button" class="enlace" (click)="f.limpiar()">Quitar filtros</button>
          }
        </span>
      </div>
    </div>
  `,
  styles: `
    .filtros {
      position: sticky;
      top: 74px;
      z-index: 10;
      display: flex;
      flex-direction: column;
      gap: 0.85rem;
      padding: 1.1rem 1.25rem;
    }
    .filtros__fila { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 0.85rem; }
    .filtros__fila--secundaria { align-items: center; }
    .filtros__total { margin-left: auto; font-size: 0.82rem; color: var(--brand-muted); }
    .filtros__total strong { color: var(--brand-ink); }
    .buscador--ancho { flex: 1 1 320px; max-width: none; }
    .buscador--ancho input { height: 44px; }
    .campo--fecha { flex: 0 1 180px; }
    .rapidos { display: inline-flex; flex-wrap: wrap; gap: 0.3rem; padding: 0.25rem; border-radius: 12px; background: var(--brand-bg); }
    .rapido {
      padding: 0.4rem 0.85rem; border: none; border-radius: 9px; background: none;
      color: var(--brand-muted); font-size: 0.8rem; font-weight: 700; cursor: pointer;
    }
    .rapido:hover { color: var(--brand-primary); }
    .rapido--activo { background: #fff; color: var(--brand-primary); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06); }
    .enlace { padding: 0; border: none; background: none; color: var(--brand-primary); font-weight: 700; cursor: pointer; }
    .enlace:hover { text-decoration: underline; }
    @media (max-width: 860px) { .filtros { position: static; } }
  `,
})
export class FiltrosSesiones {
  readonly filtro = input.required<FiltroSesiones>();

  protected readonly rangos: { valor: Rango; texto: string }[] = [
    { valor: 'todas', texto: 'Todas' },
    { valor: 'hoy', texto: 'Hoy' },
    { valor: 'semana', texto: 'Últimos 7 días' },
    { valor: 'mes', texto: 'Últimos 30 días' },
  ];
}

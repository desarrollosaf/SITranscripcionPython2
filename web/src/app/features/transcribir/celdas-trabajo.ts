import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ICellRendererAngularComp } from 'ag-grid-angular';
import { ICellRendererParams } from 'ag-grid-community';
import { EstadoTrabajo, Trabajo } from '../../core/models';

const ESTADOS: Record<EstadoTrabajo, { texto: string; clase: string }> = {
  ejecutando: { texto: 'En curso', clase: 'chip--error chip--vivo' },
  deteniendo: { texto: 'Deteniendo', clase: 'chip--warn' },
  detenido: { texto: 'Detenido', clase: '' },
  finalizado: { texto: 'Finalizado', clase: 'chip--ok' },
  error: { texto: 'Error', clase: 'chip--error' },
};

/** Chip de color según el estado del trabajo. */
@Component({
  selector: 'app-celda-estado',
  standalone: true,
  template: `<span class="chip {{ clase }}"><span class="chip__punto"></span>{{ texto }}</span>`,
  styles: `:host { display: flex; align-items: center; height: 100%; }`,
})
export class CeldaEstado implements ICellRendererAngularComp {
  protected texto = '';
  protected clase = '';

  agInit(params: ICellRendererParams<Trabajo>): void {
    const e = ESTADOS[params.data!.estado] ?? { texto: params.data!.estado, clase: '' };
    this.texto = e.texto;
    this.clase = e.clase;
  }

  refresh(params: ICellRendererParams<Trabajo>): boolean {
    this.agInit(params);
    return true;
  }
}

/** Enlace a la revisión de la sesión que va escribiendo el trabajo. */
@Component({
  selector: 'app-celda-sesion',
  standalone: true,
  imports: [RouterLink],
  template: `
    @if (sesion) {
      <a class="enlace" [routerLink]="['/sesiones', sesion]">#{{ sesion }}</a>
    } @else {
      <span class="nada">—</span>
    }
  `,
  styles: `
    :host { display: flex; align-items: center; height: 100%; }
    .enlace { color: var(--brand-primary); font-weight: 700; text-decoration: none; }
    .enlace:hover { text-decoration: underline; }
    .nada { color: var(--brand-muted); }
  `,
})
export class CeldaSesion implements ICellRendererAngularComp {
  protected sesion: number | null = null;

  agInit(params: ICellRendererParams<Trabajo>): void {
    this.sesion = params.data!.sesion_id;
  }

  refresh(params: ICellRendererParams<Trabajo>): boolean {
    this.agInit(params);
    return true;
  }
}

export interface AccionesTrabajoParams extends ICellRendererParams<Trabajo> {
  onLog: (t: Trabajo) => void;
  onDetener: (t: Trabajo) => void;
  onReanudar: (t: Trabajo) => void;
}

/** Ver log, detener o reanudar un trabajo. */
@Component({
  selector: 'app-celda-acciones-trabajo',
  standalone: true,
  template: `
    <button type="button" class="accion" (click)="params.onLog(trabajo)" title="Ver el log del transcriptor">Log</button>
    @if (activo) {
      <button type="button" class="accion accion--peligro" (click)="params.onDetener(trabajo)">Detener</button>
    } @else if (reanudable) {
      <button type="button" class="accion accion--ok" (click)="params.onReanudar(trabajo)">Reanudar</button>
    }
  `,
  styles: `
    :host { display: flex; align-items: center; gap: 0.35rem; height: 100%; }
    .accion {
      padding: 0.2rem 0.65rem; border: 1px solid var(--brand-border); border-radius: 8px;
      background: #fff; color: var(--brand-ink); font-size: 0.74rem; font-weight: 700;
      cursor: pointer; line-height: 1.6;
    }
    .accion:hover { border-color: var(--brand-primary); color: var(--brand-primary); }
    .accion--peligro { color: var(--brand-danger); border-color: rgba(179, 38, 30, 0.3); }
    .accion--peligro:hover { background: var(--brand-danger); color: #fff; border-color: var(--brand-danger); }
    .accion--ok { color: var(--ok); border-color: rgba(26, 138, 74, 0.3); }
    .accion--ok:hover { background: var(--ok); color: #fff; border-color: var(--ok); }
  `,
})
export class CeldaAccionesTrabajo implements ICellRendererAngularComp {
  protected params!: AccionesTrabajoParams;
  protected trabajo!: Trabajo;
  protected activo = false;
  protected reanudable = false;

  agInit(params: AccionesTrabajoParams): void {
    this.params = params;
    this.trabajo = params.data!;
    this.activo = this.trabajo.estado === 'ejecutando' || this.trabajo.estado === 'deteniendo';
    this.reanudable =
      ['detenido', 'error', 'finalizado'].includes(this.trabajo.estado) && !!this.trabajo.sesion_id;
  }

  refresh(params: AccionesTrabajoParams): boolean {
    this.agInit(params);
    return true;
  }
}

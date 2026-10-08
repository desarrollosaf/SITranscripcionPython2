import { Component, Signal, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { SesionesService } from '../../core/services/sesiones.service';
import { ToastService } from '../../core/services/toast.service';
import {
  FiltroSesiones,
  FiltrosSesiones,
  horaDe,
} from '../../shared/filtro-sesiones/filtro-sesiones';
import { duracionLegible } from '../../shared/utils/texto';

/** Entrada al módulo estenográfico: elegir qué sesión preparar o corregir. */
@Component({
  selector: 'app-esteno-selector',
  standalone: true,
  imports: [RouterLink, FiltrosSesiones],
  template: `
    <section class="pagina">
      <header class="pagina__header">
        <div>
          <span class="pagina__eyebrow">Módulo estenográfico</span>
          <h1>{{ esAdmin() ? 'Corrección colaborativa por tramos' : '¿Qué sesión vas a corregir?' }}</h1>
          <p>
            @if (esAdmin()) {
              Reparte una sesión en bloques de tiempo entre varios correctores: cada quien corrige su
              tramo escuchando el audio y el avance se ve aquí en tiempo real.
            } @else {
              Elige la sesión que te asignaron. Verás primero tus bloques y podrás ayudar con los libres.
            }
          </p>
        </div>
      </header>

      <app-filtros-sesiones [filtro]="filtro" />

      @if (cargando()) {
        <div class="tarjeta">
          @for (n of [1, 2, 3, 4]; track n) { <div class="skeleton" style="height: 64px; margin-bottom: 0.6rem"></div> }
        </div>
      } @else if (filtro.filtradas().length === 0) {
        <div class="tarjeta vacio">
          <span class="vacio__icono"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="9" cy="8.5" r="3" stroke="currentColor" stroke-width="1.7" /><path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5s4.9 1.5 5.5 4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" /></svg></span>
          <strong>{{ filtro.sesiones().length ? 'Ninguna sesión coincide' : 'Aún no hay sesiones' }}</strong>
          @if (filtro.sesiones().length) {
            <button type="button" class="btn btn--outline btn--sm" (click)="filtro.limpiar()">Quitar filtros</button>
          } @else {
            <span>Cuando haya una sesión en curso o terminada, aparecerá aquí.</span>
          }
        </div>
      } @else {
        @for (g of filtro.grupos(); track g.dia) {
          <div class="grupo-dia">
            <h2 class="grupo-dia__titulo">
              {{ g.titulo }}
              <span>{{ g.sesiones.length }} {{ g.sesiones.length === 1 ? 'sesión' : 'sesiones' }}</span>
            </h2>
            <ul class="lista-filas tarjeta" style="padding: 0">
              @for (s of g.sesiones; track s.id) {
                <li>
                  <a class="fila-sesion" [class.fila-sesion--vacia]="!s.segmentos" [routerLink]="['/esteno', s.id]" style="color: inherit; text-decoration: none">
                    <span class="fila-sesion__principal">
                      <span class="fila-sesion__num">#{{ s.id }}</span>
                      <span class="fila-sesion__datos">
                        <span class="fila-sesion__titulo">{{ s.titulo || 'Sesión ' + s.id }}</span>
                        <span class="fila-sesion__meta">
                          <span>{{ hora(s) }}</span>
                          <span>·</span>
                          <span>{{ s.segmentos ? duracion(s.duracion_seg) + ' de audio' : 'Sin texto' }}</span>
                        </span>
                      </span>
                    </span>
                    <span class="btn btn--outline btn--sm">{{ esAdmin() ? 'Plan y avance' : 'Corregir' }}</span>
                    <svg class="fila-sesion__flecha" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
                  </a>
                </li>
              }
            </ul>
          </div>
        }
      }
    </section>
  `,
  styles: `
    li + li .fila-sesion { border-top: 1px solid var(--brand-border); }
  `,
})
export class EstenoSelector {
  protected readonly filtro = new FiltroSesiones();
  protected readonly cargando = signal(true);
  protected readonly esAdmin: Signal<boolean>;
  protected readonly duracion = duracionLegible;
  protected readonly hora = horaDe;

  constructor(sesionesService: SesionesService, toast: ToastService, auth: AuthService) {
    this.esAdmin = auth.esAdmin;
    // Al corrector no le sirven las sesiones vacías: no tienen nada que corregir.
    if (!this.esAdmin()) this.filtro.ocultarVacias.set(true);
    sesionesService.listar().subscribe({
      next: (s) => {
        this.filtro.cargar(s);
        this.cargando.set(false);
      },
      error: (e) => {
        this.cargando.set(false);
        toast.error(mensajeDeError(e, 'No se pudieron cargar las sesiones.'));
      },
    });
  }
}

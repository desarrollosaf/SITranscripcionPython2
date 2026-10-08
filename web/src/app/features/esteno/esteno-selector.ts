import { Component, computed, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Sesion } from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { SesionesService } from '../../core/services/sesiones.service';
import { ToastService } from '../../core/services/toast.service';
import { duracionLegible, fechaCorta, normalizar } from '../../shared/utils/texto';

/** Entrada al módulo estenográfico: elegir qué sesión preparar o corregir. */
@Component({
  selector: 'app-esteno-selector',
  standalone: true,
  imports: [RouterLink],
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
        <label class="buscador">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="1.8" /><path d="m20 20-3.2-3.2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" /></svg>
          <input type="search" placeholder="Buscar sesión…" [value]="busqueda()" (input)="busqueda.set($any($event.target).value)" />
        </label>
      </header>

      @if (cargando()) {
        <div class="grid">
          @for (n of [1, 2, 3, 4]; track n) { <div class="skeleton" style="height: 120px"></div> }
        </div>
      } @else if (filtradas().length === 0) {
        <div class="tarjeta vacio">
          <span class="vacio__icono"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="9" cy="8.5" r="3" stroke="currentColor" stroke-width="1.7" /><path d="M3.5 19c.6-3 2.8-4.5 5.5-4.5s4.9 1.5 5.5 4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" /></svg></span>
          <strong>{{ sesiones().length ? 'Sin resultados' : 'Aún no hay sesiones' }}</strong>
          <span>Cuando haya una sesión en curso o terminada, aparecerá aquí.</span>
        </div>
      } @else {
        <ul class="grid">
          @for (s of filtradas(); track s.id) {
            <li>
              <a class="sesion" [routerLink]="['/esteno', s.id]">
                <span class="sesion__num">#{{ s.id }}</span>
                <span class="sesion__datos">
                  <strong>{{ s.titulo || 'Sesión ' + s.id }}</strong>
                  <small>{{ fecha(s.inicio) }} · {{ duracion(s.duracion_seg) }}</small>
                </span>
                <svg class="sesion__flecha" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
              </a>
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 0.85rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .sesion {
      display: flex;
      align-items: center;
      gap: 0.9rem;
      padding: 1rem 1.1rem;
      border: 1px solid var(--brand-border);
      border-radius: 14px;
      background: #fff;
      color: inherit;
      text-decoration: none;
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.04);
      transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
    }
    .sesion:hover {
      transform: translateY(-2px);
      border-color: rgba(150, 0, 72, 0.3);
      box-shadow: 0 14px 30px rgba(0, 0, 0, 0.08);
    }
    .sesion__num {
      display: grid;
      place-items: center;
      min-width: 48px;
      height: 48px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--brand-primary), var(--brand-primary-dark));
      color: #fff;
      font-size: 0.85rem;
      font-weight: 800;
    }
    .sesion__datos { display: flex; flex-direction: column; flex: 1; min-width: 0; }
    .sesion__datos strong {
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      font-size: 0.92rem; color: var(--brand-ink);
    }
    .sesion__datos small { font-size: 0.78rem; color: var(--brand-muted); }
    .sesion__flecha { width: 18px; height: 18px; color: #b8bcbf; }
  `,
})
export class EstenoSelector {
  protected readonly sesiones = signal<Sesion[]>([]);
  protected readonly cargando = signal(true);
  protected readonly busqueda = signal('');
  protected readonly esAdmin;
  protected readonly fecha = fechaCorta;
  protected readonly duracion = duracionLegible;

  protected readonly filtradas = computed(() => {
    const q = normalizar(this.busqueda().trim());
    return q
      ? this.sesiones().filter((s) => normalizar(`#${s.id} ${s.titulo ?? ''}`).includes(q))
      : this.sesiones();
  });

  constructor(sesionesService: SesionesService, toast: ToastService, auth: AuthService) {
    this.esAdmin = auth.esAdmin;
    sesionesService.listar().subscribe({
      next: (s) => {
        this.sesiones.set(s);
        this.cargando.set(false);
      },
      error: (e) => {
        this.cargando.set(false);
        toast.error(mensajeDeError(e, 'No se pudieron cargar las sesiones.'));
      },
    });
  }
}

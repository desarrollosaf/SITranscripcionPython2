import { Component, computed, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { forkJoin, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { EstadoLanzador, Sesion, Trabajo } from '../../core/models';
import { AuthService } from '../../core/services/auth.service';
import { SesionesService } from '../../core/services/sesiones.service';
import { TranscripcionService } from '../../core/services/transcripcion.service';
import { duracionLegible, fechaCorta } from '../../shared/utils/texto';

@Component({
  selector: 'app-inicio',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './inicio.html',
  styleUrl: './inicio.scss',
})
export class Inicio {
  protected readonly user;
  protected readonly cargando = signal(true);
  protected readonly sesiones = signal<Sesion[]>([]);
  protected readonly trabajos = signal<Trabajo[]>([]);
  protected readonly manual = signal<EstadoLanzador | null>(null);

  protected readonly recientes = computed(() => this.sesiones().slice(0, 5));
  protected readonly totalSegmentos = computed(() =>
    this.sesiones().reduce((acc, s) => acc + (s.segmentos || 0), 0),
  );
  protected readonly totalHoras = computed(() =>
    this.sesiones().reduce((acc, s) => acc + (s.duracion_seg || 0), 0),
  );
  protected readonly activos = computed(
    () => this.trabajos().filter((t) => t.estado === 'ejecutando').length + (this.manual()?.corriendo ? 1 : 0),
  );
  protected readonly saludo = (() => {
    const h = new Date().getHours();
    return h < 12 ? 'Buenos días' : h < 19 ? 'Buenas tardes' : 'Buenas noches';
  })();

  protected readonly fecha = fechaCorta;
  protected readonly duracion = duracionLegible;

  constructor(
    auth: AuthService,
    sesionesService: SesionesService,
    transcripcion: TranscripcionService,
  ) {
    this.user = auth.currentUser;
    forkJoin({
      sesiones: sesionesService.listar().pipe(catchError(() => of([] as Sesion[]))),
      trabajos: transcripcion.trabajos().pipe(catchError(() => of([] as Trabajo[]))),
      manual: transcripcion.estadoManual().pipe(catchError(() => of(null))),
    }).subscribe(({ sesiones, trabajos, manual }) => {
      this.sesiones.set(sesiones);
      this.trabajos.set(trabajos);
      this.manual.set(manual);
      this.cargando.set(false);
    });
  }
}

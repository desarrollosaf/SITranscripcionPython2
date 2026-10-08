import { Component, computed, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { Sesion } from '../../../core/models';
import { mensajeDeError } from '../../../core/interceptors/auth.interceptor';
import { SesionesService } from '../../../core/services/sesiones.service';
import { ToastService } from '../../../core/services/toast.service';
import { descargarBlob } from '../../../shared/descargar-blob';
import { duracionLegible, fechaCorta, normalizar } from '../../../shared/utils/texto';

@Component({
  selector: 'app-sesiones-lista',
  standalone: true,
  imports: [RouterLink],
  templateUrl: './sesiones-lista.html',
  styleUrl: './sesiones-lista.scss',
})
export class SesionesLista {
  protected readonly cargando = signal(true);
  protected readonly sesiones = signal<Sesion[]>([]);
  protected readonly busqueda = signal('');
  protected readonly descargando = signal<number | null>(null);

  protected readonly filtradas = computed(() => {
    const q = normalizar(this.busqueda().trim());
    if (!q) return this.sesiones();
    return this.sesiones().filter((s) =>
      normalizar(`#${s.id} ${s.titulo ?? ''} ${s.inicio ?? ''}`).includes(q),
    );
  });

  protected readonly fecha = fechaCorta;
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
      next: (s) => {
        this.sesiones.set(s);
        this.cargando.set(false);
      },
      error: (e) => {
        this.cargando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudieron cargar las sesiones.'));
      },
    });
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

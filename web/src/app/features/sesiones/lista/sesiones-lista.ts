import { Component, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { Sesion } from '../../../core/models';
import { mensajeDeError } from '../../../core/interceptors/auth.interceptor';
import { SesionesService } from '../../../core/services/sesiones.service';
import { ToastService } from '../../../core/services/toast.service';
import { descargarBlob } from '../../../shared/descargar-blob';
import {
  FiltroSesiones,
  FiltrosSesiones,
  SesionVista,
  horaDe,
} from '../../../shared/filtro-sesiones/filtro-sesiones';
import { confirmar, confirmarEscribiendo } from '../../../shared/utils/dialogos';
import { duracionLegible } from '../../../shared/utils/texto';

@Component({
  selector: 'app-sesiones-lista',
  standalone: true,
  imports: [RouterLink, FiltrosSesiones],
  templateUrl: './sesiones-lista.html',
})
export class SesionesLista {
  protected readonly filtro = new FiltroSesiones();
  protected readonly cargando = signal(true);
  protected readonly descargando = signal<number | null>(null);
  protected readonly eliminando = signal<number | null>(null);

  protected readonly duracion = duracionLegible;
  protected readonly hora = horaDe;

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
        this.filtro.cargar(lista);
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

  /**
   * Una sesión vacía se borra con una confirmación simple; una con texto
   * pide escribir su número, porque no se puede deshacer.
   */
  async eliminar(s: SesionVista): Promise<void> {
    const titulo = s.titulo || `Sesión ${s.id}`;
    const ok = s.segmentos
      ? await confirmarEscribiendo({
          titulo: `¿Eliminar la sesión #${s.id}?`,
          html:
            `<p style="margin:0 0 .6rem">${escapar(titulo)}</p>` +
            `<p style="margin:0;font-size:.9rem;color:#b3261e"><strong>Se borrarán ${s.segmentos} segmentos de texto</strong>, ` +
            'sus resúmenes y el plan de correctores. No se puede deshacer.</p>' +
            `<p style="margin:.8rem 0 0;font-size:.85rem;color:#767679">Escribe <strong>${s.id}</strong> para confirmar:</p>`,
          palabra: String(s.id),
          confirmar: 'Eliminar sesión',
        })
      : await confirmar({
          titulo: `¿Eliminar la sesión #${s.id}?`,
          texto: `${titulo} — no tiene texto transcrito.`,
          confirmar: 'Eliminar',
          peligro: true,
        });
    if (!ok) return;
    this.eliminando.set(s.id);
    this.sesionesService.eliminar(s.id).subscribe({
      next: () => {
        this.eliminando.set(null);
        this.filtro.quitar(s.id);
        this.toast.success(`Sesión #${s.id} eliminada.`);
      },
      error: (e) => {
        this.eliminando.set(null);
        this.toast.error(mensajeDeError(e, 'No se pudo eliminar la sesión.'));
      },
    });
  }

  esVideo(url: string | null): boolean {
    return !!url && url.startsWith('http');
  }
}

function escapar(s: string): string {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

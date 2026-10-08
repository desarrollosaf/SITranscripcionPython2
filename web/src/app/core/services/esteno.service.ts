import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import { BloqueAbierto, EstadoEsteno } from '../models';

/** Módulo estenográfico colaborativo (api/routers/esteno.py). */
@Injectable({ providedIn: 'root' })
export class EstenoService {
  private readonly base = `${environment.apiUrl}/esteno`;

  constructor(private readonly http: HttpClient) {}

  estado(sesion: number): Observable<EstadoEsteno> {
    return this.http.get<EstadoEsteno>(`${this.base}/${sesion}/estado`);
  }

  nombres(sesion: number): Observable<{ nombres: string[] }> {
    return this.http.get<{ nombres: string[] }>(`${this.base}/${sesion}/nombres`);
  }

  configurar(
    sesion: number,
    datos: { num_correctores: number; bloque_min: number; nombres: string[] },
  ): Observable<{ ok: boolean; bloques: number; reinicio: boolean }> {
    return this.http.post<{ ok: boolean; bloques: number; reinicio: boolean }>(
      `${this.base}/${sesion}/configurar`,
      datos,
    );
  }

  reasignar(sesion: number, indice: number, slot: number): Observable<unknown> {
    return this.http.post(`${this.base}/${sesion}/bloques/${indice}/reasignar`, { slot });
  }

  tomar(sesion: number, indice: number): Observable<BloqueAbierto> {
    return this.http.post<BloqueAbierto>(`${this.base}/${sesion}/bloques/${indice}/tomar`, {});
  }

  latido(sesion: number, indice: number): Observable<unknown> {
    return this.http.post(`${this.base}/${sesion}/bloques/${indice}/latido`, {});
  }

  soltar(sesion: number, indice: number): Observable<unknown> {
    return this.http.post(`${this.base}/${sesion}/bloques/${indice}/soltar`, {});
  }

  guardar(
    sesion: number,
    indice: number,
    segmentos: { id: number; orador: string; texto: string }[],
    terminar: boolean,
  ): Observable<{ ok: boolean; guardados: number; terminado: boolean }> {
    return this.http.post<{ ok: boolean; guardados: number; terminado: boolean }>(
      `${this.base}/${sesion}/bloques/${indice}/guardar`,
      { segmentos, terminar },
    );
  }

  vincularAudio(sesion: number, ruta: string): Observable<{ ok: boolean; existe: boolean }> {
    return this.http.post<{ ok: boolean; existe: boolean }>(`${this.base}/${sesion}/audio-ruta`, {
      ruta,
    });
  }

  urlAudio(sesion: number): string {
    return `${this.base}/${sesion}/audio`;
  }
}

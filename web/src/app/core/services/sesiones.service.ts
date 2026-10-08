import { Injectable } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import { DetalleSesion, ResultadoCambio, ResultadoResumen, Sesion } from '../models';

/** Revisión de sesiones transcritas (api/routers/sesiones.py). */
@Injectable({ providedIn: 'root' })
export class SesionesService {
  private readonly base = environment.apiUrl;

  constructor(private readonly http: HttpClient) {}

  listar(): Observable<Sesion[]> {
    return this.http.get<Sesion[]>(`${this.base}/sesiones`);
  }

  detalle(id: number): Observable<DetalleSesion> {
    return this.http.get<DetalleSesion>(`${this.base}/sesiones/${id}/participaciones`);
  }

  catalogo(): Observable<string[]> {
    return this.http.get<string[]>(`${this.base}/catalogo`);
  }

  cambiarOrador(id: number, ids: number[], orador: string): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/orador`, { ids, orador });
  }

  renombrar(id: number, de: string, a: string): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/renombrar`, { de, a });
  }

  compactar(id: number): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/compactar`, {});
  }

  guardarTextos(id: number, cambios: { id: number; texto: string }[]): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/textos`, { cambios });
  }

  dividir(
    id: number,
    datos: { ids: number[]; corte: number; orador1: string; orador2: string },
  ): Observable<{ ok: boolean; nuevo_id: number | null }> {
    return this.http.post<{ ok: boolean; nuevo_id: number | null }>(
      `${this.base}/sesiones/${id}/dividir`,
      datos,
    );
  }

  corregirEstilo(id: number): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/corregir-estilo`, {});
  }

  estructurar(id: number): Observable<ResultadoCambio> {
    return this.http.post<ResultadoCambio>(`${this.base}/sesiones/${id}/estructurar`, {});
  }

  borrarEstructura(id: number): Observable<ResultadoCambio> {
    return this.http.delete<ResultadoCambio>(`${this.base}/sesiones/${id}/estructura`);
  }

  pedirResumen(id: number, orador: string, texto: string, anclaId: number): Observable<ResultadoResumen> {
    return this.http.post<ResultadoResumen>(`${this.base}/sesiones/${id}/resumen`, {
      orador,
      texto,
      ancla_id: anclaId,
    });
  }

  borrarResumen(id: number, anclaId: number): Observable<ResultadoCambio> {
    return this.http.delete<ResultadoCambio>(`${this.base}/sesiones/${id}/resumenes/${anclaId}`);
  }

  /** URL del Word; se descarga como blob autenticado (ver descargar-blob.ts). */
  urlWord(id: number, opciones: { corrector?: string; marcarAutores?: boolean } = {}): string {
    let params = new HttpParams();
    if (opciones.corrector) params = params.set('corrector', opciones.corrector);
    if (opciones.marcarAutores) params = params.set('marcar_autores', 'true');
    const query = params.toString();
    return `${this.base}/sesiones/${id}/word${query ? '?' + query : ''}`;
  }
}

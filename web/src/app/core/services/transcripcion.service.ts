import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import {
  Contextos,
  EstadoLanzador,
  EventoCongreso,
  LogTrabajo,
  TranscripcionManual,
  Trabajo,
} from '../models';

/** Lanzar transcripciones (URL manual o evento del Congreso) y seguir los trabajos. */
@Injectable({ providedIn: 'root' })
export class TranscripcionService {
  private readonly base = environment.apiUrl;

  constructor(private readonly http: HttpClient) {}

  contextos(): Observable<Contextos> {
    return this.http.get<Contextos>(`${this.base}/lanzador/contextos`);
  }

  eventos(tipo: number): Observable<EventoCongreso[]> {
    return this.http.get<EventoCongreso[]>(`${this.base}/lanzador/eventos`, {
      params: { tipo },
    });
  }

  lanzarManual(datos: TranscripcionManual): Observable<{ ok: boolean; pid: number; aviso?: string }> {
    return this.http.post<{ ok: boolean; pid: number; aviso?: string }>(
      `${this.base}/lanzador/transcribir`,
      datos,
    );
  }

  detenerManual(): Observable<{ ok: boolean; detenida: boolean }> {
    return this.http.post<{ ok: boolean; detenida: boolean }>(`${this.base}/lanzador/detener`, {});
  }

  estadoManual(): Observable<EstadoLanzador> {
    return this.http.get<EstadoLanzador>(`${this.base}/lanzador/estado`);
  }

  crearDesdeEvento(eventoId: string, tipo: number, modelo: string): Observable<Trabajo> {
    return this.http.post<Trabajo>(`${this.base}/transcripciones/desde-evento`, {
      evento_id: eventoId,
      tipo,
      modelo,
    });
  }

  trabajos(): Observable<Trabajo[]> {
    return this.http.get<Trabajo[]>(`${this.base}/transcripciones`);
  }

  detener(id: string): Observable<Trabajo> {
    return this.http.post<Trabajo>(`${this.base}/transcripciones/${id}/detener`, {});
  }

  reanudar(id: string): Observable<Trabajo> {
    return this.http.post<Trabajo>(`${this.base}/transcripciones/${id}/reanudar`, {});
  }

  log(id: string): Observable<LogTrabajo> {
    return this.http.get<LogTrabajo>(`${this.base}/transcripciones/${id}/log`);
  }
}

import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../../environments/environment';
import { Usuario, UsuarioConPassword } from '../models';

@Injectable({ providedIn: 'root' })
export class UsuariosService {
  private readonly base = `${environment.apiUrl}/usuarios`;

  constructor(private readonly http: HttpClient) {}

  listar(): Observable<Usuario[]> {
    return this.http.get<Usuario[]>(this.base);
  }

  /** Crea la cuenta con contraseña generada por la API (se muestra una sola vez). */
  crear(email: string, nombre: string, esAdmin: boolean): Observable<UsuarioConPassword> {
    return this.http.post<UsuarioConPassword>(`${this.base}/generar`, {
      email,
      nombre,
      es_admin: esAdmin,
    });
  }

  /** El email no se edita (identifica al corrector en el módulo estenográfico). */
  editar(id: number, nombre: string, esAdmin: boolean): Observable<Usuario> {
    return this.http.put<Usuario>(`${this.base}/${id}`, { nombre, es_admin: esAdmin });
  }

  /** Contraseña nueva generada por la API (se muestra una sola vez). */
  restablecer(id: number): Observable<UsuarioConPassword> {
    return this.http.post<UsuarioConPassword>(`${this.base}/${id}/restablecer`, {});
  }
}

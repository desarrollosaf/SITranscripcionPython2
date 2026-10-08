import { Injectable, computed, signal } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable, switchMap, tap } from 'rxjs';
import { environment } from '../../../environments/environment';
import { Usuario } from '../models';

interface LoginResponse {
  access_token: string;
  token_type: string;
}

const TOKEN_KEY = 'sit_token';
const USER_KEY = 'sit_user';

@Injectable({ providedIn: 'root' })
export class AuthService {
  readonly currentUser = signal<Usuario | null>(this.readStoredUser());
  readonly esAdmin = computed(() => !!this.currentUser()?.es_admin);

  constructor(private readonly http: HttpClient) {}

  /** La API usa OAuth2 (form-urlencoded username/password) y luego /auth/me. */
  login(email: string, password: string): Observable<Usuario> {
    const body = new HttpParams().set('username', email).set('password', password);
    return this.http.post<LoginResponse>(`${environment.apiUrl}/auth/login`, body).pipe(
      tap(({ access_token }) => localStorage.setItem(TOKEN_KEY, access_token)),
      switchMap(() => this.http.get<Usuario>(`${environment.apiUrl}/auth/me`)),
      tap((usuario) => {
        localStorage.setItem(USER_KEY, JSON.stringify(usuario));
        this.currentUser.set(usuario);
      }),
    );
  }

  logout(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    this.currentUser.set(null);
  }

  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  }

  get isAuthenticated(): boolean {
    return !!this.token;
  }

  /** Para <audio src> y descargas, que no pueden mandar el header Authorization. */
  conToken(url: string): string {
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}token=${encodeURIComponent(this.token ?? '')}`;
  }

  private readStoredUser(): Usuario | null {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as Usuario) : null;
  }
}

import { Component, computed, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Usuario } from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { ToastService } from '../../core/services/toast.service';
import { UsuariosService } from '../../core/services/usuarios.service';
import { mostrarCredencial } from '../../shared/utils/dialogos';
import { normalizar } from '../../shared/utils/texto';

@Component({
  selector: 'app-usuarios',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './usuarios.html',
  styleUrl: './usuarios.scss',
})
export class Usuarios {
  protected readonly usuarios = signal<Usuario[]>([]);
  protected readonly cargando = signal(true);
  protected readonly guardando = signal(false);
  protected readonly busqueda = signal('');
  protected readonly yo;

  protected email = '';
  protected esAdmin = false;

  protected readonly filtrados = computed(() => {
    const q = normalizar(this.busqueda().trim());
    return q ? this.usuarios().filter((u) => normalizar(u.email).includes(q)) : this.usuarios();
  });
  protected readonly admins = computed(() => this.usuarios().filter((u) => u.es_admin).length);

  constructor(
    private readonly usuariosService: UsuariosService,
    private readonly toast: ToastService,
    auth: AuthService,
  ) {
    this.yo = auth.currentUser;
    this.cargar();
  }

  cargar(): void {
    this.usuariosService.listar().subscribe({
      next: (u) => {
        this.usuarios.set(u);
        this.cargando.set(false);
      },
      error: (e) => {
        this.cargando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudieron cargar los usuarios.'));
      },
    });
  }

  crear(): void {
    const email = this.email.trim().toLowerCase();
    if (!email) return;
    this.guardando.set(true);
    this.usuariosService.crear(email, this.esAdmin).subscribe({
      next: (u) => {
        this.guardando.set(false);
        this.email = '';
        this.esAdmin = false;
        mostrarCredencial(u.email, u.password);
        this.cargar();
      },
      error: (e) => {
        this.guardando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo crear la cuenta.'));
      },
    });
  }

  inicial(email: string): string {
    return email.charAt(0).toUpperCase();
  }
}

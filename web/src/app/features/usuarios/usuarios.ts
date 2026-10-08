import { Component, computed, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Usuario } from '../../core/models';
import { mensajeDeError } from '../../core/interceptors/auth.interceptor';
import { AuthService } from '../../core/services/auth.service';
import { ToastService } from '../../core/services/toast.service';
import { UsuariosService } from '../../core/services/usuarios.service';
import { confirmar, mostrarCredencial } from '../../shared/utils/dialogos';
import { normalizar } from '../../shared/utils/texto';

type FiltroRol = 'todos' | 'admin' | 'corrector';

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
  protected readonly filtroRol = signal<FiltroRol>('todos');
  protected readonly editando = signal<number | null>(null);
  protected readonly yo;

  // Alta
  protected email = '';
  protected nombre = '';
  protected esAdmin = false;

  // Edición en línea
  protected editNombre = '';
  protected editAdmin = false;

  protected readonly filtrados = computed(() => {
    const q = normalizar(this.busqueda().trim());
    const rol = this.filtroRol();
    return this.usuarios()
      .filter((u) => rol === 'todos' || (rol === 'admin') === u.es_admin)
      .filter((u) => !q || normalizar(`${u.nombre ?? ''} ${u.email}`).includes(q))
      .sort((a, b) => this.etiqueta(a).localeCompare(this.etiqueta(b), 'es'));
  });
  protected readonly admins = computed(() => this.usuarios().filter((u) => u.es_admin).length);
  protected readonly sinNombre = computed(() => this.usuarios().filter((u) => !u.nombre).length);

  constructor(
    private readonly usuariosService: UsuariosService,
    private readonly toast: ToastService,
    private readonly auth: AuthService,
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

  etiqueta(u: Usuario): string {
    return u.nombre || u.email;
  }

  iniciales(u: Usuario): string {
    const base = (u.nombre || u.email.split('@')[0]).trim();
    const partes = base.split(/[\s._-]+/).filter(Boolean);
    return ((partes[0]?.[0] ?? '') + (partes[1]?.[0] ?? '')).toUpperCase() || '?';
  }

  crear(): void {
    const email = this.email.trim().toLowerCase();
    if (!email) return;
    this.guardando.set(true);
    this.usuariosService.crear(email, this.nombre.trim(), this.esAdmin).subscribe({
      next: (u) => {
        this.guardando.set(false);
        this.email = '';
        this.nombre = '';
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

  editar(u: Usuario): void {
    this.editNombre = u.nombre ?? '';
    this.editAdmin = u.es_admin;
    this.editando.set(u.id);
  }

  guardar(u: Usuario): void {
    this.guardando.set(true);
    this.usuariosService.editar(u.id, this.editNombre.trim(), this.editAdmin).subscribe({
      next: (actualizado) => {
        this.guardando.set(false);
        this.editando.set(null);
        this.usuarios.update((lista) => lista.map((x) => (x.id === u.id ? actualizado : x)));
        // Si me edité a mí mismo, que la barra superior muestre el nombre nuevo.
        if (u.id === this.yo()?.id) {
          this.auth.currentUser.set(actualizado);
          localStorage.setItem('sit_user', JSON.stringify(actualizado));
        }
        this.toast.success('Usuario actualizado.');
      },
      error: (e) => {
        this.guardando.set(false);
        this.toast.error(mensajeDeError(e, 'No se pudo guardar.'));
      },
    });
  }

  async restablecer(u: Usuario): Promise<void> {
    const ok = await confirmar({
      titulo: '¿Generar una contraseña nueva?',
      texto: `La contraseña actual de ${this.etiqueta(u)} dejará de funcionar de inmediato.`,
      confirmar: 'Generar',
      peligro: true,
    });
    if (!ok) return;
    this.usuariosService.restablecer(u.id).subscribe({
      next: (r) => mostrarCredencial(r.email, r.password, 'Contraseña nueva'),
      error: (e) => this.toast.error(mensajeDeError(e, 'No se pudo restablecer la contraseña.')),
    });
  }
}

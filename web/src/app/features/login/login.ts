import { Component, signal } from '@angular/core';
import { ReactiveFormsModule, FormBuilder, Validators } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { AuthService } from '../../core/services/auth.service';
import { ToastService } from '../../core/services/toast.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [ReactiveFormsModule],
  templateUrl: './login.html',
  styleUrl: './login.scss',
})
export class Login {
  protected readonly showPassword = signal(false);
  protected readonly loading = signal(false);

  protected readonly form;

  constructor(
    private readonly fb: FormBuilder,
    private readonly authService: AuthService,
    private readonly router: Router,
    private readonly toastService: ToastService,
    route: ActivatedRoute,
  ) {
    this.form = this.fb.group({
      email: ['', [Validators.required]],
      password: ['', [Validators.required]],
    });

    const motivo = route.snapshot.queryParamMap.get('motivo');
    if (motivo === 'inactividad') {
      this.toastService.info('Tu sesión se cerró por inactividad. Vuelve a iniciar sesión.');
    } else if (motivo === 'sesion') {
      this.toastService.info('Tu sesión expiró o ya no es válida. Vuelve a iniciar sesión.');
    }
  }

  togglePassword(): void {
    this.showPassword.update((value) => !value);
  }

  submit(): void {
    if (this.form.invalid || this.loading()) {
      this.form.markAllAsTouched();
      return;
    }

    const { email, password } = this.form.getRawValue();
    this.loading.set(true);

    this.authService.login(email!.trim().toLowerCase(), password!).subscribe({
      next: (usuario) => {
        this.loading.set(false);
        void this.router.navigateByUrl(usuario.es_admin ? '/inicio' : '/esteno');
      },
      error: (err: HttpErrorResponse) => {
        this.loading.set(false);
        this.authService.logout();
        this.toastService.error(
          err.status === 401
            ? 'Email o contraseña incorrectos.'
            : 'No se pudo iniciar sesión. Intenta de nuevo más tarde.',
        );
      },
    });
  }
}

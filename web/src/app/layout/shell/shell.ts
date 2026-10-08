import { Component, OnDestroy, OnInit, computed } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { forkJoin, interval, of } from 'rxjs';
import { catchError, map, startWith, switchMap } from 'rxjs/operators';
import { AuthService } from '../../core/services/auth.service';
import { InactivityService } from '../../core/services/inactivity.service';
import { TranscripcionService } from '../../core/services/transcripcion.service';

const INTERVALO_EN_VIVO_MS = 15000;

type Icono = 'inicio' | 'sesiones' | 'transcribir' | 'esteno' | 'usuarios';

interface NavItem {
  label: string;
  route: string;
  icono: Icono;
  soloAdmin: boolean;
}

const NAV: NavItem[] = [
  { label: 'Inicio', route: '/inicio', icono: 'inicio', soloAdmin: true },
  { label: 'Sesiones', route: '/sesiones', icono: 'sesiones', soloAdmin: true },
  { label: 'Transcribir', route: '/transcribir', icono: 'transcribir', soloAdmin: true },
  { label: 'Estenográfico', route: '/esteno', icono: 'esteno', soloAdmin: false },
  { label: 'Usuarios', route: '/usuarios', icono: 'usuarios', soloAdmin: true },
];

@Component({
  selector: 'app-shell',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  templateUrl: './shell.html',
  styleUrl: './shell.scss',
})
export class Shell implements OnInit, OnDestroy {
  protected readonly user;
  protected readonly nav;
  /** Cuántas transcripciones están corriendo ahora (lanzador manual + trabajos de la API). */
  protected readonly enVivo;

  constructor(
    private readonly authService: AuthService,
    private readonly router: Router,
    private readonly transcripcion: TranscripcionService,
    protected readonly inactivity: InactivityService,
  ) {
    this.user = this.authService.currentUser;
    this.nav = computed(() => {
      const admin = this.authService.esAdmin();
      return NAV.filter((n) => admin || !n.soloAdmin);
    });

    const activos$ = interval(INTERVALO_EN_VIVO_MS).pipe(
      startWith(0),
      switchMap(() =>
        this.authService.esAdmin()
          ? forkJoin({
              manual: this.transcripcion.estadoManual(),
              trabajos: this.transcripcion.trabajos(),
            }).pipe(
              map(
                ({ manual, trabajos }) =>
                  (manual.corriendo ? 1 : 0) +
                  trabajos.filter((t) => t.estado === 'ejecutando').length,
              ),
              catchError(() => of(0)),
            )
          : of(0),
      ),
    );
    this.enVivo = toSignal(activos$, { initialValue: 0 });
  }

  ngOnInit(): void {
    this.inactivity.start();
  }

  ngOnDestroy(): void {
    this.inactivity.stop();
  }

  logout(): void {
    this.inactivity.stop();
    this.authService.logout();
    void this.router.navigateByUrl('/login');
  }

  cerrarMenuMovil(): void {
    const menu = document.getElementById('mainNav');
    if (!menu || !menu.classList.contains('show')) return;

    const bootstrapGlobal = (window as unknown as { bootstrap?: BootstrapGlobal }).bootstrap;
    bootstrapGlobal?.Collapse.getOrCreateInstance(menu).hide();
  }
}

interface BootstrapGlobal {
  Collapse: {
    getOrCreateInstance(element: Element): { hide(): void };
  };
}

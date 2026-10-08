import { Injectable, NgZone, signal } from '@angular/core';
import { Router } from '@angular/router';
import { AuthService } from './auth.service';

const IDLE_LIMIT_MS = 60 * 60 * 1000;
const WARNING_LEAD_MS = 60 * 1000;
const ACTIVITY_EVENTS = ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'click'] as const;

@Injectable({ providedIn: 'root' })
export class InactivityService {
  readonly showWarning = signal(false);
  readonly secondsRemaining = signal(0);

  private lastActivity = Date.now();
  private tickHandle: ReturnType<typeof setInterval> | null = null;
  private readonly boundOnActivity = () => this.onActivity();

  constructor(
    private readonly authService: AuthService,
    private readonly router: Router,
    private readonly ngZone: NgZone,
  ) {}

  start(): void {
    if (this.tickHandle) {
      return;
    }

    this.lastActivity = Date.now();
    this.showWarning.set(false);

    this.ngZone.runOutsideAngular(() => {
      ACTIVITY_EVENTS.forEach((evt) =>
        document.addEventListener(evt, this.boundOnActivity, { passive: true }));
      this.tickHandle = setInterval(() => this.tick(), 1000);
    });
  }

  stop(): void {
    ACTIVITY_EVENTS.forEach((evt) => document.removeEventListener(evt, this.boundOnActivity));

    if (this.tickHandle) {
      clearInterval(this.tickHandle);
      this.tickHandle = null;
    }

    this.showWarning.set(false);
  }

  staySignedIn(): void {
    this.lastActivity = Date.now();
    this.showWarning.set(false);
  }

  private onActivity(): void {
    this.lastActivity = Date.now();

    if (this.showWarning()) {
      this.ngZone.run(() => this.showWarning.set(false));
    }
  }

  private tick(): void {
    const elapsed = Date.now() - this.lastActivity;

    if (elapsed >= IDLE_LIMIT_MS) {
      this.ngZone.run(() => this.logoutByInactivity());
      return;
    }

    const remaining = IDLE_LIMIT_MS - elapsed;

    if (remaining <= WARNING_LEAD_MS) {
      const seconds = Math.ceil(remaining / 1000);
      this.ngZone.run(() => {
        this.showWarning.set(true);
        this.secondsRemaining.set(seconds);
      });
    }
  }

  private logoutByInactivity(): void {
    this.stop();
    this.authService.logout();
    void this.router.navigate(['/login'], { queryParams: { motivo: 'inactividad' } });
  }
}

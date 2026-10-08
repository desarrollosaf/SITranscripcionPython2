import { Injectable, signal } from '@angular/core';

export type ToastTipo = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  tipo: ToastTipo;
  mensaje: string;
}

const DURACION_MS = 4500;

@Injectable({ providedIn: 'root' })
export class ToastService {
  readonly toasts = signal<Toast[]>([]);

  private nextId = 1;

  success(mensaje: string): void {
    this.show(mensaje, 'success');
  }

  error(mensaje: string): void {
    this.show(mensaje, 'error');
  }

  info(mensaje: string): void {
    this.show(mensaje, 'info');
  }

  dismiss(id: number): void {
    this.toasts.update((rows) => rows.filter((t) => t.id !== id));
  }

  private show(mensaje: string, tipo: ToastTipo): void {
    const id = this.nextId++;
    this.toasts.update((rows) => [...rows, { id, tipo, mensaje }]);
    setTimeout(() => this.dismiss(id), DURACION_MS);
  }
}

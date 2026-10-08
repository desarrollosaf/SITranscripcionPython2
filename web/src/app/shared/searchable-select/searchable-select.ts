import { Component, computed, effect, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

export interface SearchableOpcion {
  id: number;
  label: string;
}

@Component({
  selector: 'app-searchable-select',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './searchable-select.html',
  styleUrl: './searchable-select.scss',
})
export class SearchableSelect {
  readonly opciones = input<SearchableOpcion[]>([]);
  readonly valor = input<number | null>(null);
  readonly placeholder = input('Selecciona una opción');
  readonly disabled = input(false);
  readonly invalid = input(false);

  readonly valorChange = output<number>();

  protected readonly query = signal('');
  protected readonly open = signal(false);

  protected readonly resultados = computed(() => {
    if (!this.open()) {
      return [];
    }
    const term = this.query().trim().toLowerCase();
    const opts = this.opciones();
    return term ? opts.filter((o) => o.label.toLowerCase().includes(term)) : opts;
  });

  constructor() {
    effect(() => {
      const valor = this.valor();
      const opciones = this.opciones();
      if (this.open()) {
        return;
      }
      this.query.set(opciones.find((o) => o.id === valor)?.label ?? '');
    });
  }

  onQueryChange(valor: string): void {
    this.query.set(valor);
  }

  onFocus(): void {
    if (this.disabled()) {
      return;
    }
    this.open.set(true);
    this.query.set('');
  }

  onBlur(): void {
    setTimeout(() => {
      this.open.set(false);
      this.query.set(this.opciones().find((o) => o.id === this.valor())?.label ?? '');
    }, 150);
  }

  seleccionar(opcion: SearchableOpcion): void {
    this.query.set(opcion.label);
    this.open.set(false);
    this.valorChange.emit(opcion.id);
  }
}

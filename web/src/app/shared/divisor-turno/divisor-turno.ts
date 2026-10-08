import { Component, computed, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { frasesDe } from '../utils/texto';

export interface Division {
  corte: number;
  orador1: string;
  orador2: string;
}

let siguienteId = 0;

/**
 * Separa un turno en dos oradores: se hace clic en la frase donde empieza el
 * segundo y se escriben ambos nombres. Emite el corte en caracteres sobre el
 * texto unido con espacios, que es lo que espera POST /sesiones/{id}/dividir.
 */
@Component({
  selector: 'app-divisor-turno',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="divisor">
      @if (frases().length < 2) {
        <p class="divisor__ayuda">Este turno es muy corto para dividirlo por frases.</p>
        <button type="button" class="btn btn--outline btn--sm" (click)="cancelar.emit()">Cerrar</button>
      } @else {
        <p class="divisor__ayuda">
          Haz clic en la frase donde empieza el <strong>segundo orador</strong>:
        </p>
        <div class="divisor__frases">
          @for (f of frases(); track f.offset; let k = $index) {
            <button
              type="button"
              class="frase"
              [class.frase--segunda]="indiceCorte() !== null && k >= indiceCorte()!"
              (click)="elegir(k)"
            >{{ f.texto }}</button>
          }
        </div>

        @if (indiceCorte() !== null) {
          <datalist [id]="idLista">
            @for (n of nombres(); track n) {
              <option [value]="n"></option>
            }
          </datalist>
          <div class="divisor__oradores">
            <label class="campo">
              <span class="campo__label">1ª parte</span>
              <input [attr.list]="idLista" [(ngModel)]="orador1" />
            </label>
            <label class="campo">
              <span class="campo__label">2ª parte</span>
              <input [attr.list]="idLista" [(ngModel)]="orador2" placeholder="¿Quién habla después?" />
            </label>
            <button
              type="button"
              class="btn btn--primary btn--sm"
              [disabled]="!orador1.trim() || !orador2.trim() || ocupado()"
              (click)="confirmar()"
            >
              Dividir aquí
            </button>
            <button type="button" class="btn btn--outline btn--sm" (click)="cancelar.emit()">Cancelar</button>
          </div>
        } @else {
          <button type="button" class="btn btn--outline btn--sm" (click)="cancelar.emit()">Cancelar</button>
        }
      }
    </div>
  `,
  styles: `
    .divisor {
      margin-top: 0.85rem;
      padding: 1rem;
      border: 1px dashed rgba(150, 0, 72, 0.35);
      border-radius: 12px;
      background: #fcf7f9;
    }
    .divisor__ayuda {
      margin: 0 0 0.6rem;
      font-size: 0.84rem;
      color: var(--brand-muted);
    }
    .divisor__frases {
      font: 15px/1.9 var(--texto-serif);
      margin-bottom: 0.75rem;
    }
    .frase {
      display: inline;
      padding: 2px 4px;
      margin: 0 1px;
      border: none;
      border-radius: 5px;
      background: none;
      color: var(--brand-ink);
      font: inherit;
      text-align: left;
      cursor: pointer;
      transition: background 0.12s;
    }
    .frase:hover { background: var(--brand-primary-soft); }
    .frase--segunda { background: #f4e7ce; }
    .divisor__oradores {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: 0.75rem;
    }
    .divisor__oradores .campo { flex: 1 1 220px; }
  `,
})
export class DivisorTurno {
  readonly texto = input.required<string>();
  readonly oradorInicial = input('');
  readonly nombres = input<string[]>([]);
  readonly ocupado = input(false);

  readonly dividir = output<Division>();
  readonly cancelar = output<void>();

  protected readonly idLista = `divisor-nombres-${siguienteId++}`;
  protected readonly frases = computed(() => frasesDe(this.texto()));
  protected readonly indiceCorte = signal<number | null>(null);
  protected orador1 = '';
  protected orador2 = '';

  elegir(k: number): void {
    if (k === 0) return; // la primera frase siempre es del primer orador
    if (this.indiceCorte() === null) this.orador1 = this.oradorInicial();
    this.indiceCorte.set(k);
  }

  confirmar(): void {
    const k = this.indiceCorte();
    if (k === null) return;
    this.dividir.emit({
      corte: this.frases()[k].offset,
      orador1: this.orador1.trim(),
      orador2: this.orador2.trim(),
    });
  }
}

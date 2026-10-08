import Swal from 'sweetalert2';

/** Confirmación con el estilo de la marca. Resuelve true si el usuario acepta. */
export async function confirmar(opciones: {
  titulo: string;
  texto: string;
  confirmar?: string;
  peligro?: boolean;
}): Promise<boolean> {
  const r = await Swal.fire({
    title: opciones.titulo,
    text: opciones.texto,
    icon: opciones.peligro ? 'warning' : 'question',
    showCancelButton: true,
    reverseButtons: true,
    confirmButtonText: opciones.confirmar ?? 'Continuar',
    cancelButtonText: 'Cancelar',
    confirmButtonColor: opciones.peligro ? '#b3261e' : 'rgb(150, 0, 72)',
    cancelButtonColor: '#767679',
  });
  return r.isConfirmed;
}

/**
 * Confirmación para algo que no se puede deshacer: hay que escribir
 * `palabra` para que se habilite el botón.
 */
export async function confirmarEscribiendo(opciones: {
  titulo: string;
  html: string;
  palabra: string;
  confirmar: string;
}): Promise<boolean> {
  const r = await Swal.fire({
    title: opciones.titulo,
    html: opciones.html,
    icon: 'warning',
    input: 'text',
    inputPlaceholder: opciones.palabra,
    inputAttributes: { autocomplete: 'off', autocapitalize: 'off' },
    showCancelButton: true,
    reverseButtons: true,
    confirmButtonText: opciones.confirmar,
    cancelButtonText: 'Cancelar',
    confirmButtonColor: '#b3261e',
    cancelButtonColor: '#767679',
    preConfirm: (valor: string) => {
      if ((valor || '').trim() !== opciones.palabra) {
        Swal.showValidationMessage(`Escribe «${opciones.palabra}» para confirmar`);
        return false;
      }
      return true;
    },
  });
  return r.isConfirmed;
}

/** Muestra una credencial recién creada, que no se vuelve a poder ver. */
export function mostrarCredencial(email: string, password: string, titulo = 'Cuenta creada'): void {
  void Swal.fire({
    title: titulo,
    icon: 'success',
    html:
      '<p style="margin:0 0 .8rem;font-size:.9rem;color:#767679">Copia esta contraseña ahora: ' +
      'no se vuelve a mostrar. Si se pierde, genera otra con el botón «Contraseña».</p>' +
      `<p style="margin:.3rem 0">Usuario: <code class="credencial">${escapar(email)}</code></p>` +
      `<p style="margin:.3rem 0">Contraseña: <code class="credencial">${escapar(password)}</code></p>`,
    confirmButtonText: 'Ya la copié',
    confirmButtonColor: 'rgb(150, 0, 72)',
  });
}

function escapar(s: string): string {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

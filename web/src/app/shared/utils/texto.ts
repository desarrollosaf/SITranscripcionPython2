/** Utilidades de texto y tiempo compartidas por revisión y módulo estenográfico. */

/** 3725 → "01:02:05". Con `corto`, omite la hora si es cero ("02:05"). */
export function hms(segundos: number | null | undefined, corto = false): string {
  const s = Math.max(0, Math.floor(segundos ?? 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const p = (n: number) => String(n).padStart(2, '0');
  return corto && h === 0 ? `${p(m)}:${p(s % 60)}` : `${p(h)}:${p(m)}:${p(s % 60)}`;
}

/** Duración legible: "1 h 05 min", "12 min", "45 s". */
export function duracionLegible(segundos: number | null | undefined): string {
  const s = Math.max(0, Math.round(segundos ?? 0));
  if (s < 60) return `${s} s`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min`;
}

/** Minúsculas y sin acentos, para buscar sin importar cómo se escribió. */
export function normalizar(texto: string): string {
  return (texto || '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase();
}

/**
 * El módulo estenográfico guarda texto con formato (HTML de su editor). La
 * pantalla de revisión trabaja con texto plano: esto quita las etiquetas.
 */
export function htmlAPlano(texto: string | null | undefined): string {
  if (!texto || texto.indexOf('<') === -1) return texto || '';
  const d = document.createElement('div');
  d.innerHTML = texto;
  d.querySelectorAll('div,p,br,li').forEach((el) => el.insertAdjacentText('beforebegin', ' '));
  return (d.textContent || '').replace(/\s+/g, ' ').trim();
}

function escapar(s: string): string {
  return s.replace(
    /[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string,
  );
}

/** Markdown sencillo del resumen de IA (títulos, negritas, listas) → HTML seguro. */
export function markdownAHtml(texto: string): string {
  const enLinea = (s: string) =>
    escapar(s)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*]+)\*/g, '<em>$1</em>');
  let html = '';
  let enLista = false;
  const cerrarLista = () => {
    if (enLista) {
      html += '</ul>';
      enLista = false;
    }
  };
  for (const cruda of (texto || '').replace(/\r/g, '').split('\n')) {
    const l = cruda.trim();
    if (!l) {
      cerrarLista();
      continue;
    }
    if (/^(-{3,}|_{3,}|\*{3,})$/.test(l)) {
      cerrarLista();
      html += '<hr>';
      continue;
    }
    let m: RegExpMatchArray | null;
    if ((m = l.match(/^(#{1,6})\s+(.*)$/))) {
      cerrarLista();
      const n = Math.min(m[1].length + 2, 6);
      html += `<h${n}>${enLinea(m[2])}</h${n}>`;
    } else if ((m = l.match(/^[-*•]\s+(.*)$/))) {
      if (!enLista) {
        html += '<ul>';
        enLista = true;
      }
      html += `<li>${enLinea(m[1])}</li>`;
    } else {
      cerrarLista();
      html += `<p>${enLinea(l)}</p>`;
    }
  }
  cerrarLista();
  return html;
}

export interface Frase {
  texto: string;
  /** Desplazamiento en caracteres dentro del texto completo (lo que espera /dividir). */
  offset: number;
}

/** Parte un texto en frases conservando dónde empieza cada una. */
export function frasesDe(texto: string): Frase[] {
  const frases: Frase[] = [];
  const rex = /[^.?!]+[.?!]*\s*/g;
  let m: RegExpExecArray | null;
  while ((m = rex.exec(texto)) !== null) {
    if (m[0].trim()) frases.push({ texto: m[0].trim(), offset: m.index });
    if (rex.lastIndex === m.index) rex.lastIndex++;
  }
  return frases;
}

/** Enlace a YouTube en el segundo indicado, si la sesión viene de un video. */
export function enlaceVideo(url: string | null | undefined, segundos: number): string | null {
  if (!url || !url.startsWith('http')) return null;
  return `${url}${url.includes('?') ? '&' : '?'}t=${Math.floor(segundos)}s`;
}

/** "2026-10-08T14:37:01" → "8 oct 2026, 14:37". */
export function fechaCorta(valor: string | null | undefined): string {
  if (!valor) return '—';
  const d = new Date(valor.replace(' ', 'T'));
  if (isNaN(d.getTime())) return valor;
  return d.toLocaleString('es-MX', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

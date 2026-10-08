import { HttpClient, HttpResponse } from '@angular/common/http';
import { Observable, map } from 'rxjs';

/**
 * Descarga un archivo generado por el backend (blob autenticado). Toma el
 * nombre del Content-Disposition si viene; si no, usa `nombrePorDefecto`.
 */
export function descargarBlob(
  http: HttpClient,
  url: string,
  nombrePorDefecto: string,
): Observable<void> {
  return http.get(url, { responseType: 'blob', observe: 'response' }).pipe(
    map((resp: HttpResponse<Blob>) => {
      const objectUrl = URL.createObjectURL(resp.body as Blob);
      const enlace = document.createElement('a');
      enlace.href = objectUrl;
      enlace.download = nombreDeCabecera(resp.headers.get('Content-Disposition')) ?? nombrePorDefecto;
      enlace.click();
      URL.revokeObjectURL(objectUrl);
    }),
  );
}

function nombreDeCabecera(cabecera: string | null): string | null {
  if (!cabecera) return null;
  const utf8 = cabecera.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8) return decodeURIComponent(utf8[1]);
  const simple = cabecera.match(/filename="?([^";]+)"?/i);
  return simple ? simple[1] : null;
}

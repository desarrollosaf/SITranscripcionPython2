/** Tipos de lo que devuelve la API de FastAPI (api/). */

export interface Usuario {
  id: number;
  email: string;
  es_admin: boolean;
}

export interface UsuarioConPassword extends Usuario {
  password: string;
}

export interface Sesion {
  id: number;
  url: string | null;
  titulo: string | null;
  inicio: string | null;
  fin: string | null;
  segmentos: number;
  duracion_seg: number | null;
}

export interface Fila {
  id: number;
  orador: string;
  inicio_seg: number;
  fin_seg: number;
  texto: string;
  voz_orador?: string | null;
  voz_similitud?: number | null;
  revisado_ia?: 'validado' | 'media' | 'descartado' | null;
  motivo_ia?: string | null;
}

export interface SeccionOrden {
  ancla_id: number;
  numero: number;
  titulo: string;
}

export interface DetalleSesion {
  sesion: Sesion;
  filas: Fila[];
  resumenes: Record<string, string>;
  estructura: { secciones: SeccionOrden[] } | null;
}

export interface ResultadoCambio {
  ok: boolean;
  cambios?: number;
  unidos?: number;
  aviso?: string;
  secciones?: number;
}

export interface ResultadoResumen {
  modo: 'auto' | 'copiar';
  resumen?: string;
  prompt?: string;
  error?: string;
}

export type EstadoTrabajo = 'ejecutando' | 'deteniendo' | 'detenido' | 'finalizado' | 'error';

export interface Trabajo {
  id: string;
  url: string;
  estado: EstadoTrabajo;
  fuente: 'youtube' | 'srt';
  puerto: number | null;
  passphrase: string | null;
  participantes_pedidos: string[];
  participantes_encontrados: string[];
  participantes_no_encontrados: string[];
  sesion_id: number | null;
  error: string | null;
  creado_en: string;
}

export interface LogTrabajo {
  id: string;
  estado: EstadoTrabajo;
  lineas: string;
}

export interface EventoCongreso {
  id: string;
  descripcion: string;
  fecha: string;
  tipoevento: string | null;
  liga: string | null;
  comisiones: string[];
  participantes: number;
}

export interface Contextos {
  disponible: boolean;
  comisiones: string[];
  tiene_transcriptor: boolean;
  archivo: string;
  error?: string;
}

export interface EstadoLanzador {
  corriendo: boolean;
  pid: number | null;
  codigo: number | null;
  lineas: string;
}

export interface TranscripcionManual {
  url: string;
  tipo: 'pleno' | 'comision';
  comisiones: string[];
  fecha: string;
  modelo: string;
}

export interface Bloque {
  indice: number;
  inicio_seg: number;
  fin_seg: number;
  slot: number;
  estado: 'pendiente' | 'editando' | 'terminado';
  tomado_por: string | null;
  terminado_por: string | null;
  vencido: boolean;
  segmentos: number;
  listo: boolean;
}

export interface ConfigEsteno {
  num_correctores: number;
  bloque_seg: number;
  nombres: string[];
}

export interface EstadoEsteno {
  sesion: Pick<Sesion, 'id' | 'titulo' | 'inicio' | 'url'> | null;
  config: ConfigEsteno | null;
  total_seg: number;
  bloques: Bloque[];
  en_vivo: boolean;
  progreso: { terminados: number; total: number };
  audio: boolean;
}

export interface Segmento {
  id: number;
  orador: string | null;
  inicio_seg: number;
  fin_seg: number;
  inicio_hms: string | null;
  texto: string | null;
}

export interface BloqueAbierto {
  segmentos: Segmento[];
  bloque: Pick<Bloque, 'indice' | 'inicio_seg' | 'fin_seg' | 'slot'>;
}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
corregir.py
===========
Segunda pasada (corrector) sobre una sesión YA transcrita en sesiones.db.

Reevalúa los tramos que quedaron como Mesa/Presidencia/Secretaría/Desconocido
apoyándose en la evidencia de voz que ya guardó la primera pasada
(voz_orador / voz_similitud) y en el contexto de los oradores vecinos, y
NARRA en consola cada decisión: qué encontró, qué decidió y por qué. Escribe
el veredicto (validado / descartado) y el motivo que revisar.py muestra como
insignia junto a cada bloque.

No necesita audio, ni modelos, ni internet: trabaja sobre lo que ya está en
la base. (El corrector EN VIVO de transcribir_en_vivo.py --corrector es más
preciso porque re-analiza el audio del turno completo; este script es la
versión retro, para sesiones que ya se transcribieron sin él.)

Uso:
    python corregir.py                 # última sesión, SOLO muestra (no toca la base)
    python corregir.py --sesion 3      # una sesión concreta
    python corregir.py --aplicar       # además guarda las insignias y reasigna
    python corregir.py --umbral-voz 0.70   # exigir más o menos voz para asignar

Sin --aplicar es una simulación: verás todo lo que haría, pero la base no se
modifica. Cuando te convenza, repite con --aplicar.
"""

import argparse
import os
import sqlite3
import sys

# Etiquetas "zona gris": las únicas que el corrector se permite tocar. Un
# orador ya nombrado (o respaldado por anuncio) es intocable.
ORADOR_MESA = "Presidencia / Mesa Directiva"
ORADOR_DESCONOCIDO = "Desconocido"
ORADOR_SECRETARIA = "Secretaría"
GRISES = {ORADOR_MESA, ORADOR_DESCONOCIDO, ORADOR_SECRETARIA}

SEG_MICRO = 3.0        # un tramo de <= 3 s es un micro-corte
SIM_SANDWICH = 0.55    # parecido moderado que, con contexto, ya basta


def hms(segundos):
    segundos = int(segundos or 0)
    h, r = divmod(segundos, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _col(fila, nombre, defecto=None):
    return fila[nombre] if nombre in fila.keys() else defecto


def asegurar_columnas(con):
    """Añade las columnas del corrector si la base es anterior a ellas."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(participaciones)")}
    for col, ddl in (("fuente", "fuente TEXT DEFAULT 'rapido'"),
                     ("revisado_ia", "revisado_ia TEXT"),
                     ("motivo_ia", "motivo_ia TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE participaciones ADD COLUMN {ddl}")
    con.commit()


def agrupar(filas):
    """Tramos = filas consecutivas con el mismo orador. Cada tramo se queda
    con la mejor evidencia de voz de sus filas y su duración total."""
    turnos = []
    t = None
    for f in filas:
        if t is None or t["orador"] != f["orador"]:
            t = {"orador": f["orador"], "ids": [], "voz": None, "sim": 0.0,
                 "ini": f["inicio_seg"], "fin": f["fin_seg"], "seg": 0.0,
                 "texto": [], "revisado": _col(f, "revisado_ia")}
            turnos.append(t)
        t["ids"].append(f["id"])
        t["fin"] = f["fin_seg"]
        t["seg"] += max(0.0, (f["fin_seg"] or 0) - (f["inicio_seg"] or 0))
        if f["texto"]:
            t["texto"].append(f["texto"])
        vo = _col(f, "voz_orador")
        vs = _col(f, "voz_similitud", 0) or 0
        if vo and vs > t["sim"]:
            t["voz"], t["sim"] = vo, vs
    return turnos


def decidir(t, izq, der, umbral_alto, umbral_medio):
    """Regla del corrector sobre la evidencia guardada. Tres niveles de
    confianza por la similitud de voz:
      · alta  (>= umbral_alto): se valida sola.
      · media (umbral_medio..umbral_alto): se valida SOLO si el contexto la
        respalda (la voz apunta a un orador vecino).
      · baja  (< umbral_medio): se descarta / se deja para revisión.
    Devuelve (veredicto, nuevo_orador_o_None, motivo). Veredictos:
    'validado', 'media', 'descartado', 'omitir'."""
    actual = t["orador"]
    cand, sim = t["voz"], t["sim"]
    li = izq["orador"] if izq else None
    ld = der["orador"] if der else None
    micro = t["seg"] <= SEG_MICRO

    # 1) Confianza ALTA: la voz apunta con fuerza a OTRO orador
    if cand and cand != actual and sim >= umbral_alto:
        return ("validado", cand,
                f"voz {sim:.2f} ≥ {umbral_alto:.2f} (confianza alta): "
                f"corresponde a {cand}")

    # 2) Sándwich: el MISMO orador (nombrado) habla justo antes y después.
    #    Es contexto puro, se acepta aunque la voz sea floja o falte.
    if li and li == ld and li not in GRISES:
        if micro:
            return ("validado", li,
                    f"micro-corte de {t['seg']:.1f}s entre dos turnos de "
                    f"{li}: fue una pausa, no otra persona")
        if cand == li and sim >= SIM_SANDWICH:
            return ("validado", li,
                    f"turno continuo con {li}: su voz ({sim:.2f}) concuerda "
                    "con la de los vecinos")

    # 3) Sin evidencia con la que decidir: NO se marca (queda sin insignia =
    #    "pendiente de revisión humana"), en vez de estampar un ✗ vacío
    if not cand:
        return ("omitir", None,
                "sin evidencia de voz registrada: lo dejo para tu revisión")

    # 4) Confianza MEDIA: la voz cae en la franja intermedia. Solo se valida
    #    si el contexto la respalda, es decir, si el orador que sugiere la
    #    voz también aparece pegado (antes o después): eso es corroboración
    #    independiente de que este tramo es de esa misma persona.
    if cand != actual and umbral_medio <= sim < umbral_alto:
        if cand == li or cand == ld:
            lado = "antes" if cand == li else "después"
            return ("media", cand,
                    f"voz {sim:.2f} (franja media {umbral_medio:.2f}–"
                    f"{umbral_alto:.2f}) respaldada por el contexto: {cand} "
                    f"habla justo {lado}")
        return ("descartado", None,
                f"voz {sim:.2f} en la franja media pero sin respaldo del "
                f"contexto (los vecinos no son {cand})")

    # 5) Confianza BAJA o sin contexto: se deja como estaba y se explica
    if sim < umbral_medio:
        return ("descartado", None,
                f"la voz más parecida ({cand}) llega a {sim:.2f}, por "
                f"debajo de la franja media ({umbral_medio:.2f})")
    if not (li and li == ld):
        return ("descartado", None,
                f"la voz apunta a {cand} ({sim:.2f}) pero los oradores "
                "vecinos no coinciden, no hay contexto que lo respalde")
    return ("descartado", None, "sin evidencia suficiente para cambiarlo")


def recorte(textos, n=72):
    t = " ".join(textos).strip().replace("\n", " ")
    return (t[:n] + "…") if len(t) > n else t


def main():
    ap = argparse.ArgumentParser(
        description="Segunda pasada (corrector) sobre una sesión ya "
                    "transcrita: reevalúa Mesa/Presidencia/Secretaría/"
                    "Desconocido y narra cada decisión.")
    ap.add_argument("--db", default="sesiones.db",
                    help="Base de datos SQLite (default: sesiones.db)")
    ap.add_argument("--sesion", type=int, default=0,
                    help="id de la sesión (0 = la última registrada)")
    ap.add_argument("--umbral-voz", type=float, default=0.75,
                    help="confianza ALTA: similitud a partir de la cual se "
                         "valida sola, sin necesitar contexto (default: 0.75)")
    ap.add_argument("--umbral-medio", type=float, default=0.68,
                    help="piso de la franja MEDIA: entre este valor y "
                         "--umbral-voz, se valida solo si el contexto lo "
                         "respalda; por debajo, se deja para tu revisión "
                         "(default: 0.68)")
    ap.add_argument("--aplicar", action="store_true",
                    help="guarda las insignias y reasigna los oradores; sin "
                         "esto solo muestra lo que haría, sin tocar la base")
    args = ap.parse_args()

    # La franja media debe quedar por debajo de la alta; si no, se ignora
    # (equivale a no tener franja media).
    if args.umbral_medio > args.umbral_voz:
        print(f"[aviso] --umbral-medio ({args.umbral_medio:.2f}) es mayor que "
              f"--umbral-voz ({args.umbral_voz:.2f}); se iguala a este último "
              "(sin franja media).")
        args.umbral_medio = args.umbral_voz

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(args.db):
        print(f"No encuentro la base de datos: {os.path.abspath(args.db)}")
        sys.exit(1)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    if args.aplicar:
        asegurar_columnas(con)

    sid = args.sesion or (con.execute(
        "SELECT id FROM sesiones ORDER BY id DESC LIMIT 1").fetchone()
        or [0])[0]
    ses = con.execute("SELECT * FROM sesiones WHERE id=?", (sid,)).fetchone()
    if not ses:
        print(f"No existe la sesión #{sid}.")
        sys.exit(1)

    filas = list(con.execute(
        "SELECT * FROM participaciones WHERE sesion_id=? "
        "ORDER BY inicio_seg, id", (sid,)))
    turnos = agrupar(filas)

    tiene_voz = any(_col(f, "voz_orador") for f in filas)
    print("=" * 66)
    print(f"CORRECTOR — sesión #{sid}: {_col(ses, 'titulo') or ''}")
    print(f"{len(turnos)} tramos · confianza alta ≥{args.umbral_voz:.2f} · "
          f"media {args.umbral_medio:.2f}–{args.umbral_voz:.2f} · "
          + ("APLICANDO cambios" if args.aplicar
             else "SIMULACIÓN (no se toca la base)"))
    if not tiene_voz:
        print("AVISO: esta sesión no tiene evidencia de voz guardada "
              "(¿se transcribió sin --voz?); casi todo quedará 'descartado'.")
    print("=" * 66)

    n_val = n_media = n_desc = n_omit = n_saltados = 0
    for i, t in enumerate(turnos):
        if t["orador"] not in GRISES:
            n_saltados += 1
            continue
        izq = turnos[i - 1] if i - 1 >= 0 else None
        der = turnos[i + 1] if i + 1 < len(turnos) else None
        veredicto, nuevo, motivo = decidir(
            t, izq, der, args.umbral_voz, args.umbral_medio)

        print("\n" + "─" * 66)
        print(f"[{hms(t['ini'])}]  {t['orador']}    ({t['seg']:.0f}s)")
        print(f"   texto    «{recorte(t['texto'])}»")
        if t["voz"]:
            print(f"   voz reg. {t['voz']}  ({t['sim']:.2f})")
        else:
            print("   voz reg. (sin evidencia)")
        print(f"   vecinos  ← {izq['orador'] if izq else '—'}"
              f"    → {der['orador'] if der else '—'}")
        if t["revisado"]:
            print(f"   (ya tenía veredicto previo: {t['revisado']})")
        if veredicto == "validado":
            n_val += 1
            print(f"   ✓ VALIDADO → {nuevo}")
        elif veredicto == "media":
            n_media += 1
            print(f"   ◐ VALIDADO (media confianza) → {nuevo}")
        elif veredicto == "descartado":
            n_desc += 1
            print("   ✗ DESCARTADO (se deja como está)")
        else:  # omitir
            n_omit += 1
            print("   — SIN MARCAR (pendiente de tu revisión)")
        print(f"   motivo:  {motivo}")

        if args.aplicar and veredicto != "omitir":
            marcas = ",".join("?" * len(t["ids"]))
            if veredicto in ("validado", "media"):
                con.execute(
                    f"UPDATE participaciones SET orador=?, fuente='corregido',"
                    f" revisado_ia=?, motivo_ia=? "
                    f"WHERE id IN ({marcas})",
                    [nuevo, veredicto, motivo] + t["ids"])
            else:
                con.execute(
                    f"UPDATE participaciones SET revisado_ia='descartado', "
                    f"motivo_ia=? WHERE id IN ({marcas})",
                    [motivo] + t["ids"])
            con.commit()

    print("\n" + "=" * 66)
    print(f"RESUMEN: {n_val} validados · {n_media} media confianza · "
          f"{n_desc} descartados · {n_omit} sin marcar · "
          f"{n_saltados} ya nombrados")
    if not args.aplicar and (n_val or n_media or n_desc):
        print("Fue una SIMULACIÓN. Revisa sobre todo los ◐ de media "
              "confianza; si cuadran, repite con --aplicar para guardarlo.")
    elif args.aplicar:
        print("Guardado. Abre revisar.py: ✓ validado / ◐ media confianza / "
              "✗ descartado por IA aparecen junto a cada bloque.")
    print("=" * 66)
    con.close()


if __name__ == "__main__":
    main()

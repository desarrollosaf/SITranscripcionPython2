#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auditar_perfiles.py
===================
Compara TODOS los perfiles de voz entre sí y reporta los pares demasiado
parecidos. Dos perfiles "gemelos" hacen que la identificación por voz de
esos diputados quede frenada por margen insuficiente en TODAS las
sesiones, aunque la similitud sea alta.

Uso:
    python auditar_perfiles.py [voces_perfiles.json] [--umbral 0.75]

Interpretación (similitud coseno entre perfiles):
    >= 0.85  casi seguro hay audio mal etiquetado o compartido: regenerar
    0.75-0.85 sospechoso: revisar el audio fuente de ambos
    <  0.75  normal (voces distintas)
"""

import argparse
import json
import sys

try:
    import numpy as np
except ImportError:
    sys.exit("Falta numpy: pip install numpy")


def main():
    ap = argparse.ArgumentParser(
        description="Detecta perfiles de voz demasiado parecidos entre sí.")
    ap.add_argument("perfiles", nargs="?", default="voces_perfiles.json",
                    help="archivo de huellas (default: voces_perfiles.json)")
    ap.add_argument("--umbral", type=float, default=0.75,
                    help="similitud entre perfiles a partir de la cual se "
                         "reporta el par (default: 0.75)")
    args = ap.parse_args()

    try:
        with open(args.perfiles, encoding="utf-8") as f:
            perfiles = json.load(f)
    except OSError as e:
        sys.exit(f"No se pudo abrir {args.perfiles}: {e}")

    nombres = list(perfiles.keys())
    if len(nombres) < 2:
        sys.exit("Se necesitan al menos 2 perfiles para comparar.")

    matriz = []
    for n in nombres:
        v = np.asarray(perfiles[n]["embedding"], dtype=np.float32)
        norma = np.linalg.norm(v)
        matriz.append(v / norma if norma else v)
    matriz = np.stack(matriz)

    sims = matriz @ matriz.T
    pares = []
    for i in range(len(nombres)):
        for j in range(i + 1, len(nombres)):
            pares.append((float(sims[i, j]), nombres[i], nombres[j]))
    pares.sort(reverse=True)

    print(f"Perfiles analizados: {len(nombres)} "
          f"({len(pares)} pares comparados)\n")
    sospechosos = [p for p in pares if p[0] >= args.umbral]
    if not sospechosos:
        print(f"Ningún par supera {args.umbral:.2f} de similitud. "
              "Los perfiles se distinguen bien entre sí.")
    else:
        print(f"PARES CON SIMILITUD >= {args.umbral:.2f} "
              "(de mayor a menor):\n")
        for sim, a, b in sospechosos:
            marca = "!!" if sim >= 0.85 else " ?"
            print(f"  [{marca}] {sim:.3f}  {a}  <->  {b}")
        print("\n[!!] casi gemelos: regenerar ambos con voz.py usando "
              "audio limpio,\n     verificando que ningún fragmento esté "
              "mal etiquetado ni compartido.")
        print("[ ?] sospechosos: revisar el audio fuente de ambos.")

    # Contexto: los 5 pares más parecidos aunque no lleguen al umbral
    print("\nLos 5 pares más parecidos en general:")
    for sim, a, b in pares[:5]:
        print(f"        {sim:.3f}  {a}  <->  {b}")


if __name__ == "__main__":
    main()

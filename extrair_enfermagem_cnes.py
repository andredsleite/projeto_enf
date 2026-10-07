"""Extrai do FTP do CNES (DATASUS) os profissionais de enfermagem da ultima competencia disponivel.

Fonte: ftp.datasus.gov.br /dissemin/publicos/CNES/200508_/Dados/PF/PF{UF}{AAMM}.dbc
Saida: CSV com um registro por vinculo profissional (CNES x profissional x CBO).

Uso:
    python extrair_enfermagem_cnes.py                 # todas as UFs, ultima competencia
    python extrair_enfermagem_cnes.py --uf SP RJ      # apenas algumas UFs
    python extrair_enfermagem_cnes.py --competencia 2025-06
"""
import argparse
import ftplib
import re
import sys
from pathlib import Path

import pandas as pd
from dbfread import DBF
from datasus_dbc import decompress  # pacote datasus-dbc

FTP_HOST = "ftp.datasus.gov.br"
FTP_DIR = "/dissemin/publicos/CNES/200508_/Dados/PF"
UFS = ("AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO").split()

# CBO de enfermagem: 2235 = enfermeiros (todas as especialidades);
# 3222 = tecnicos e auxiliares de enfermagem.
CBO_PREFIXOS = ("2235", "3222")

NOME_ARQ = re.compile(r"^PF([A-Z]{2})(\d{2})(\d{2})\.dbc$", re.IGNORECASE)


def listar_arquivos(ftp):
    """Retorna {(uf, 'AAAA-MM'): nome_arquivo}."""
    ftp.cwd(FTP_DIR)
    arquivos = {}
    for nome in ftp.nlst():
        m = NOME_ARQ.match(nome.split("/")[-1])
        if m:
            uf, aa, mm = m.groups()
            arquivos[(uf.upper(), f"20{aa}-{mm}")] = nome.split("/")[-1]
    return arquivos


def baixar(ftp, nome, destino: Path):
    if destino.exists():
        return
    with open(destino, "wb") as f:
        ftp.retrbinary(f"RETR {nome}", f.write)


def ler_enfermagem(dbc: Path, manter_dbf: bool = False) -> pd.DataFrame:
    dbf = dbc.with_suffix(".dbf")
    if not dbf.exists():
        decompress(str(dbc), str(dbf))  # etapa .dbc -> .dbf
    # Le so os registros de enfermagem para economizar memoria
    regs = (
        r for r in DBF(str(dbf), encoding="latin-1", load=False, char_decode_errors="replace")
        if str(r.get("CBO", "")).startswith(CBO_PREFIXOS)
    )
    df = pd.DataFrame(regs)
    if not manter_dbf:
        dbf.unlink(missing_ok=True)
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uf", nargs="+", default=UFS, help="UFs a extrair (padrao: todas)")
    ap.add_argument("--competencia", help="AAAA-MM (padrao: a ultima disponivel no FTP)")
    ap.add_argument("--saida", default="saida", help="pasta de saida")
    ap.add_argument("--cache", default="cache_ftp", help="pasta para os .dbc baixados")
    ap.add_argument("--manter-dbf", action="store_true",
                    help="mantem o .dbf completo (todas as ocupacoes) na pasta de cache")
    args = ap.parse_args()

    ufs = [u.upper() for u in args.uf]
    saida, cache = Path(args.saida), Path(args.cache)
    saida.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)

    with ftplib.FTP(FTP_HOST, timeout=120) as ftp:
        ftp.login()
        arquivos = listar_arquivos(ftp)

        competencia = args.competencia
        if not competencia:
            # Ultima competencia presente para TODAS as UFs pedidas (evita carga parcial)
            comps = {c for (_, c) in arquivos}
            completas = [c for c in comps if all((u, c) in arquivos for u in ufs)]
            if not completas:
                sys.exit("Nenhuma competencia disponivel para todas as UFs pedidas.")
            competencia = max(completas)
        print(f"Competencia: {competencia}")

        partes = []
        for uf in ufs:
            nome = arquivos.get((uf, competencia))
            if not nome:
                print(f"  {uf}: arquivo nao encontrado, pulando")
                continue
            print(f"  {uf}: baixando {nome}")
            destino = cache / nome
            baixar(ftp, nome, destino)
            df = ler_enfermagem(destino, args.manter_dbf)
            df.insert(0, "UF_ARQUIVO", uf)
            partes.append(df)
            print(f"  {uf}: {len(df)} vinculos de enfermagem")

    if not partes:
        sys.exit("Nada extraido.")
    final = pd.concat(partes, ignore_index=True)
    final.insert(1, "COMPETENCIA_ARQUIVO", competencia)
    arq = saida / f"enfermagem_cnes_{competencia}.csv"
    try:
        final.to_csv(arq, index=False, encoding="utf-8-sig", sep=";")
    except PermissionError:
        # Arquivo aberto no Excel (ou travado por antivirus/OneDrive): grava com outro nome
        arq = arq.with_name(f"{arq.stem}_novo.csv")
        print(f"Arquivo original em uso; gravando como {arq.name}")
        final.to_csv(arq, index=False, encoding="utf-8-sig", sep=";")
    print(f"Gravado: {arq} ({len(final)} linhas)")


if __name__ == "__main__":
    main()

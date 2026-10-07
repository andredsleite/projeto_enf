"""Vincula os profissionais de enfermagem (saida de extrair_enfermagem_cnes.py) aos
estabelecimentos do CNES, com coordenadas validadas.

Fonte dos estabelecimentos: https://cnes.datasus.gov.br/pages/downloads/arquivosBaseDados.jsp
    EstatisticasServlet?path=BASE_DE_DADOS_CNES_AAAAMM.ZIP  -> tbEstabelecimentoAAAAMM.csv

Saidas (pasta --saida):
    enfermagem_estabelecimentos_AAAA-MM.csv   um registro por vinculo, com dados do estabelecimento
    estabelecimentos_enfermagem_AAAA-MM.gpkg  um ponto por estabelecimento (so os com coordenada valida),
                                              com contagem de vinculos/enfermeiros/tecnicos-auxiliares

Uso:
    python vincular_estabelecimentos_cnes.py
    python vincular_estabelecimentos_cnes.py --entrada saida/enfermagem_cnes_2026-08.csv
"""
import argparse
import glob
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

URL_ZIP = "https://cnes.datasus.gov.br/EstatisticasServlet?path=BASE_DE_DADOS_CNES_{aaaamm}.ZIP"

# Nomes de coluna aceitos (o CNES ja mudou nomes entre versoes). Primeiro que existir vence.
COLUNAS = {
    "cnes": ["CO_CNES"],
    "lat": ["NU_LATITUDE"],
    "lon": ["NU_LONGITUDE"],
    "fantasia": ["NO_FANTASIA"],
    "razao": ["NO_RAZAO_SOCIAL"],
    "tipo": ["CO_TIPO_UNIDADE"],
    "municipio": ["CO_MUNICIPIO_GESTOR"],
    "uf": ["CO_ESTADO_GESTOR"],
    "logradouro": ["NO_LOGRADOURO"],
    "numero": ["NU_ENDERECO"],
    "bairro": ["NO_BAIRRO"],
    "cep": ["CO_CEP"],
}
OBRIGATORIAS = ("cnes", "lat", "lon")

# Caixa que contem o territorio brasileiro (inclui ilhas oceanicas)
LAT_MIN, LAT_MAX = -34.0, 6.0
LON_MIN, LON_MAX = -74.5, -28.0


def dentro_do_brasil(lat, lon):
    return lat.between(LAT_MIN, LAT_MAX) & lon.between(LON_MIN, LON_MAX)


def para_numero(serie: pd.Series) -> pd.Series:
    return pd.to_numeric(serie.astype(str).str.strip().str.replace(",", ".", regex=False), errors="coerce")


def validar_coordenadas(df: pd.DataFrame) -> pd.DataFrame:
    """Acrescenta LAT, LON e COORD_STATUS (ok | corrigida_* | ausente | invalida)."""
    lat, lon = para_numero(df["_lat"]), para_numero(df["_lon"])
    status = pd.Series("invalida", index=df.index)
    status[lat.isna() | lon.isna() | ((lat == 0) & (lon == 0))] = "ausente"

    ok = dentro_do_brasil(lat, lon)
    status[ok] = "ok"

    # Latitude e longitude trocadas
    troca = ~ok & status.eq("invalida") & dentro_do_brasil(lon, lat)
    lat.loc[troca], lon.loc[troca] = lon[troca].copy(), lat[troca].copy()
    status[troca] = "corrigida_troca_lat_lon"

    # Sinal perdido (hemisferio sul / oeste): positivo -> negativo
    for nome, la, lo in (("corrigida_sinal_lat", -lat, lon), ("corrigida_sinal_lon", lat, -lon),
                         ("corrigida_sinal_lat_lon", -lat, -lon)):
        cand = status.eq("invalida") & dentro_do_brasil(la, lo)
        lat.loc[cand], lon.loc[cand] = la[cand], lo[cand]
        status[cand] = nome

    out = df.copy()
    out["LAT"], out["LON"], out["COORD_STATUS"] = lat, lon, status
    out.loc[~status.str.startswith(("ok", "corrigida")), ["LAT", "LON"]] = float("nan")
    return out


def baixar_zip(competencia: str, cache: Path):
    """Baixa o zip da competencia; se nao existir, tenta ate 6 meses anteriores."""
    ano, mes = int(competencia[:4]), int(competencia[5:7])
    for _ in range(7):
        aaaamm = f"{ano}{mes:02d}"
        destino = cache / f"BASE_DE_DADOS_CNES_{aaaamm}.ZIP"
        if destino.exists() and zipfile.is_zipfile(destino):
            return destino, aaaamm
        print(f"Tentando baixar base CNES {aaaamm} ...")
        try:
            with urllib.request.urlopen(URL_ZIP.format(aaaamm=aaaamm), timeout=120) as r:
                cabeca = r.read(4)
                if cabeca[:2] != b"PK":
                    raise urllib.error.URLError("resposta nao e um zip")
                with open(destino, "wb") as f:
                    f.write(cabeca)
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
            return destino, aaaamm
        except (urllib.error.URLError, OSError) as e:
            print(f"  indisponivel ({e})")
            destino.unlink(missing_ok=True)
            mes -= 1
            if mes == 0:
                ano, mes = ano - 1, 12
    sys.exit("Nao consegui baixar a base de estabelecimentos do CNES.")


def extrair_csv_estabelecimento(zip_path: Path, cache: Path) -> Path:
    with zipfile.ZipFile(zip_path) as z:
        alvo = [n for n in z.namelist() if re.search(r"tbEstabelecimento\d*\.csv$", n, re.IGNORECASE)]
        if not alvo:
            sys.exit(f"tbEstabelecimento*.csv nao encontrado no zip. Conteudo: {z.namelist()[:40]}")
        destino = cache / Path(alvo[0]).name
        if not destino.exists():
            with z.open(alvo[0]) as src, open(destino, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
    return destino


def ler_estabelecimentos(csv: Path, cnes_alvo: set) -> pd.DataFrame:
    for enc in ("utf-8", "latin-1"):
        try:
            cab = pd.read_csv(csv, sep=";", nrows=0, dtype=str, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    cab.columns = [c.strip().strip('"') for c in cab.columns]

    mapa = {}
    for chave, candidatas in COLUNAS.items():
        achada = next((c for c in candidatas if c in cab.columns), None)
        if achada:
            mapa[chave] = achada
    faltam = [k for k in OBRIGATORIAS if k not in mapa]
    if faltam:
        sys.exit(f"Colunas obrigatorias nao encontradas {faltam}. Colunas do arquivo: {list(cab.columns)}")

    partes = []
    for chunk in pd.read_csv(csv, sep=";", dtype=str, encoding=enc, usecols=list(mapa.values()),
                             chunksize=200_000, on_bad_lines="skip"):
        chunk.columns = [c.strip().strip('"') for c in chunk.columns]
        chunk = chunk.rename(columns={v: k for k, v in mapa.items()})
        chunk["cnes"] = chunk["cnes"].str.strip().str.zfill(7)
        partes.append(chunk[chunk["cnes"].isin(cnes_alvo)])
    est = pd.concat(partes, ignore_index=True).drop_duplicates("cnes")
    return est.rename(columns={"lat": "_lat", "lon": "_lon"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entrada", help="CSV de enfermagem (padrao: o mais recente em saida/)")
    ap.add_argument("--saida", default="saida")
    ap.add_argument("--cache", default="cache_cnes")
    args = ap.parse_args()

    entrada = args.entrada
    if not entrada:
        achados = sorted(glob.glob(str(Path(args.saida) / "enfermagem_cnes_*.csv")))
        if not achados:
            sys.exit("Nenhum CSV de enfermagem em saida/. Rode antes extrair_enfermagem_cnes.py.")
        entrada = achados[-1]
    print(f"Entrada: {entrada}")

    prof = pd.read_csv(entrada, sep=";", dtype=str, encoding="utf-8-sig")
    prof["CNES"] = prof["CNES"].str.strip().str.zfill(7)
    competencia = prof["COMPETENCIA_ARQUIVO"].iloc[0]
    print(f"{len(prof)} vinculos, {prof['CNES'].nunique()} estabelecimentos, competencia {competencia}")

    cache = Path(args.cache)
    cache.mkdir(exist_ok=True)
    saida = Path(args.saida)
    saida.mkdir(exist_ok=True)

    zip_path, aaaamm = baixar_zip(competencia, cache)
    csv = extrair_csv_estabelecimento(zip_path, cache)
    est = ler_estabelecimentos(csv, set(prof["CNES"]))
    est = validar_coordenadas(est)
    print("Situacao das coordenadas (estabelecimentos):")
    print(est["COORD_STATUS"].value_counts().to_string())

    est = est.drop(columns=["_lat", "_lon"]).rename(columns=str.upper)

    # --- CSV por vinculo
    juntado = prof.merge(est, on="CNES", how="left", indicator="_merge")
    sem_cadastro = (juntado["_merge"] == "left_only").sum()
    juntado = juntado.drop(columns="_merge")
    if sem_cadastro:
        print(f"Aviso: {sem_cadastro} vinculos sem estabelecimento correspondente na base CNES {aaaamm}")
    arq_csv = saida / f"enfermagem_estabelecimentos_{competencia}.csv"
    juntado.to_csv(arq_csv, index=False, encoding="utf-8-sig", sep=";")

    # --- GeoPackage por estabelecimento
    cbo = prof["CBO"].fillna("")
    resumo = (
        prof.assign(
            ENFERMEIRO=cbo.str.startswith("2235"),
            TEC_AUX=cbo.str.startswith("3222"),
        )
        .groupby("CNES")
        .agg(N_VINCULOS=("CBO", "size"), N_ENFERMEIROS=("ENFERMEIRO", "sum"), N_TEC_AUX=("TEC_AUX", "sum"))
        .reset_index()
    )
    pontos = est.merge(resumo, on="CNES", how="inner")
    pontos = pontos[pontos["LAT"].notna()]
    gdf = gpd.GeoDataFrame(pontos, geometry=gpd.points_from_xy(pontos["LON"], pontos["LAT"]), crs="EPSG:4326")
    # fiona nao entende o dtype de texto do pandas novo: converte para object
    for col in gdf.columns.drop("geometry"):
        if not pd.api.types.is_numeric_dtype(gdf[col]):
            gdf[col] = gdf[col].astype(object).where(gdf[col].notna(), None)
    arq_gpkg = saida / f"estabelecimentos_enfermagem_{competencia}.gpkg"
    arq_gpkg.unlink(missing_ok=True)
    try:
        import pyogrio  # noqa: F401  (fiona quebra com NumPy 2)
    except ImportError:
        sys.exit("Falta o pyogrio para gravar o GeoPackage: pip install pyogrio")
    gdf.to_file(arq_gpkg, layer="estabelecimentos_enfermagem", driver="GPKG", engine="pyogrio")

    print(f"CSV:     {arq_csv} ({len(juntado)} linhas)")
    print(f"GPKG:    {arq_gpkg} ({len(gdf)} estabelecimentos com coordenada valida de {len(resumo)})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""PMU V27: archives détaillées, résultats isolés, rapports non interprétés.

Les réponses historiques peuvent inclure des données post-course. Ne pas utiliser les
arrivées, temps, rapports définitifs ou cotes finales comme variables explicatives.
Utilisation à des fins de recherche non commerciale, sous réserve des CGU applicables.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_HOSTS = (
    'https://offline.turfinfo.api.pmu.fr/rest/client/7',
    'https://online.turfinfo.api.pmu.fr/rest/client/1',
)
REQ_DELAY = 0.24
TIMEOUT = 16

REUNIONS = ('date', 'reunion', 'hippodrome', 'pays', 'nb_courses_annoncees', 'source')
COURSES = (
    'date', 'reunion', 'course', 'hippodrome', 'discipline', 'specialite',
    'distance_m', 'heure_depart_utc', 'nb_partants_annonces', 'prix',
    'participants_recuperes', 'rapports_recuperes', 'arrivee_complete_disponible',
)
CHEVAUX = (
    'date', 'reunion', 'course', 'numero', 'nom', 'age', 'sexe',
    'statut_historique', 'jockey', 'driver', 'entraineur', 'proprietaire',
    'musique_historique', 'nombre_courses_historique', 'nombre_victoires_historique',
    'nombre_places_historique', 'poids_handicap_kg', 'valeur_handicap',
    'place_corde', 'distance_handicap_m', 'deferre',
    'rapport_reference_brut', 'horodatage_reference_utc',
    'minutes_reference_avant_depart', 'reference_horodatee_avant_depart',
    'historique_apres_course_non_certifie',
)
ARRIVEES = ('date', 'reunion', 'course', 'numero', 'nom', 'rang', 'statut_historique')
RAPPORTS = (
    'date', 'reunion', 'course', 'type_pari', 'libelle',
    'combinaison', 'mise_base_brute', 'dividende_brut',
    'dividende_pour_un_euro_brut', 'unite',
)
COUVERTURE = (
    'date', 'programme_ok', 'reunions_annoncees', 'courses_annoncees',
    'participants_ok', 'rapports_ok', 'chevaux', 'arrivees_renseignees', 'erreurs',
)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def readable_ms(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def asfloat(value):
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def normal_country(value):
    if isinstance(value, dict):
        value = value.get('code') or value.get('libelle')
    value = str(value or 'FR').strip().upper()
    return {'FRA':'FR', 'FRANCE':'FR', 'GBR':'GB', 'DEU':'GER', 'IRL':'IRE'}.get(value, value)


def str_hippodrome(value):
    if isinstance(value, dict):
        return str(value.get('libelleCourt') or value.get('libelleLong') or value.get('nom') or 'Inconnu')
    return str(value or 'Inconnu')


def as_rows(programme):
    root = programme.get('programme', programme) if isinstance(programme, dict) else None
    if not isinstance(root, dict) or not isinstance(root.get('reunions'), list):
        raise ValueError('programme.reunions manquant ou invalide')
    return root['reunions']


class ApiClient:
    def __init__(self, offline_zip=None):
        self.zip = zipfile.ZipFile(offline_zip) if offline_zip else None
        self.last_request = 0

    def read(self, relpath, zip_path):
        """Retourne (objet, origine, erreur). Absence jamais assimilée à une réussite."""
        if self.zip:
            try:
                return json.loads(self.zip.read(zip_path)), 'archive de test', None
            except KeyError:
                return None, None, f'Absent dans ZIP: {zip_path}'
            except (ValueError, UnicodeError) as err:
                return None, None, f'JSON ZIP invalide: {zip_path} ({err})'
        messages = []
        for host in API_HOSTS:
            for attempt in range(2):
                waiting = REQ_DELAY - (time.monotonic() - self.last_request)
                if waiting > 0:
                    time.sleep(waiting)
                self.last_request = time.monotonic()
                url = host + relpath
                request = Request(url, headers={
                    'Accept': 'application/json',
                    'User-Agent': 'PMU-Research-Archive/1.0 (noncommercial; bounded requests)',
                })
                try:
                    with urlopen(request, timeout=TIMEOUT) as response:
                        if response.status != 200:
                            raise ValueError(f'HTTP {response.status}')
                        payload = response.read(15_000_000)
                    result = json.loads(payload)
                    if not isinstance(result, (list, dict)):
                        raise ValueError('JSON racine ni objet ni tableau')
                    return result, url, None
                except HTTPError as err:
                    messages.append(f'HTTP {err.code}: {url}')
                    if err.code not in (429, 500, 502, 503, 504):
                        break
                except (URLError, TimeoutError, OSError, ValueError) as err:
                    messages.append(f'{type(err).__name__}: {url} ({str(err)[:110]})')
                if attempt == 0:
                    time.sleep(1.0)
        return None, None, ' | '.join(messages[-4:])

    def close(self):
        if self.zip:
            self.zip.close()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def horse_record(d, rn, cn, p, departure):
    num = p.get('numPmu')
    ref = p.get('dernierRapportReference')
    ref = ref if isinstance(ref, dict) else {}
    ref_ms = asfloat(ref.get('dateRapport'))
    dep_ms = asfloat(departure)
    delta = round((dep_ms - ref_ms) / 60_000, 2) if dep_ms is not None and ref_ms is not None else None
    # Les rapports de référence horodatés peuvent être antérieurs au départ,
    # sans prouver que toutes les autres variables étaient disponibles à cet instant.
    valid_time = delta is not None and 0 <= delta <= 180
    results = {
        'date': d, 'reunion': rn, 'course': cn, 'numero': num,
        'nom': p.get('nom'), 'age': p.get('age'), 'sexe': p.get('sexe'),
        'statut_historique': p.get('statut'),
        'jockey': p.get('jockey'), 'driver': p.get('driver'),
        'entraineur': p.get('entraineur'), 'proprietaire': p.get('proprietaire'),
        'musique_historique': p.get('musique'),
        'nombre_courses_historique': p.get('nombreCourses'),
        'nombre_victoires_historique': p.get('nombreVictoires'),
        'nombre_places_historique': p.get('nombrePlaces'),
        'poids_handicap_kg': p.get('handicapPoids'),
        'valeur_handicap': p.get('handicapValeur'),
        'place_corde': p.get('placeCorde'),
        'distance_handicap_m': p.get('handicapDistance'),
        'deferre': p.get('deferre'),
        'rapport_reference_brut': ref.get('rapport') if valid_time else None,
        'horodatage_reference_utc': readable_ms(ref_ms) if valid_time else '',
        'minutes_reference_avant_depart': delta if valid_time else None,
        'reference_horodatee_avant_depart': int(bool(valid_time)),
        'historique_apres_course_non_certifie': 1,
    }
    return results


def collect(start, end, dest, offline_zip=None, pays_filtre="FR"):
    if end < start or (end - start).days > 6:
        raise ValueError('Une collecte doit couvrir entre 1 et 7 jours inclus.')
    dest.mkdir(parents=True, exist_ok=True)
    client = ApiClient(offline_zip)
    day_audit, reunions_out, courses_out, chevaux_out, arrivees_out, rapports_out = [], [], [], [], [], []
    errors = []
    try:
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            iso, pmu = day.isoformat(), day.strftime('%d%m%Y')
            day_folder = dest / iso
            audit = {'date':iso, 'programme_ok':0, 'reunions_annoncees':0,
                     'courses_annoncees':0, 'participants_ok':0,
                     'rapports_ok':0, 'chevaux':0, 'arrivees_renseignees':0, 'erreurs':0}
            path = f'/programme/{pmu}'
            data, source, error = client.read(path, f'{iso}/programme.json')
            if data is None:
                errors.append({'date':iso, 'etape':'programme', 'message':error})
                audit['erreurs'] += 1
                day_audit.append(audit)
                print(f'{iso}: ECHEC programme: {error}', flush=True)
                continue
            try:
                meetings = as_rows(data)
            except ValueError as exc:
                errors.append({'date':iso, 'etape':'validation_programme', 'message':str(exc)})
                audit['erreurs'] += 1
                day_audit.append(audit)
                continue
            write_json(day_folder / 'programme.json', data)
            audit['programme_ok'] = 1
            # En pratique l'API historique ne fournit souvent les fiches détail
            # que pour les réunions françaises. Par défaut ignorer les autres pays.
            selected = [m for m in meetings if pays_filtre == 'TOUS' or
                        normal_country(m.get('pays')) == pays_filtre]
            audit['reunions_annoncees'] = len(selected)
            for meeting in selected:
                rn = meeting.get('numOfficiel')
                if rn is None:
                    errors.append({'date':iso, 'etape':'reunion', 'message':'numOfficiel absent'})
                    audit['erreurs'] += 1
                    continue
                hip = str_hippodrome(meeting.get('hippodrome'))
                country = normal_country(meeting.get('pays'))
                races = meeting.get('courses', [])
                if not isinstance(races, list):
                    races = []
                reunions_out.append({'date':iso, 'reunion':rn, 'hippodrome':hip,
                                     'pays':country, 'nb_courses_annoncees':len(races), 'source':source})
                for race in races:
                    cn = race.get('numOrdre')
                    if cn is None:
                        errors.append({'date':iso, 'reunion':rn, 'etape':'course', 'message':'numOrdre absent'})
                        audit['erreurs'] += 1
                        continue
                    audit['courses_annoncees'] += 1
                    race_folder = day_folder / f'R{rn}' / f'C{cn}'
                    depart = race.get('heureDepart')
                    meta = {
                        'date':iso, 'reunion':rn, 'course':cn, 'hippodrome':hip,
                        'discipline':race.get('discipline'), 'specialite':race.get('specialite'),
                        'distance_m':race.get('distance'), 'heure_depart_utc':readable_ms(depart),
                        'nb_partants_annonces':race.get('nombreDeclaresPartants'),
                        'prix':race.get('libelle'),
                        'participants_recuperes':0, 'rapports_recuperes':0,
                        'arrivee_complete_disponible':0,
                    }
                    part, url, err = client.read(
                        f'/programme/{pmu}/R{rn}/C{cn}/participants',
                        f'{iso}/R{rn}/C{cn}/participants.json',
                    )
                    if isinstance(part, dict) and isinstance(part.get('participants'), list):
                        write_json(race_folder / 'participants.json', part)
                        audit['participants_ok'] += 1
                        meta['participants_recuperes'] = 1
                        rank_rows = []
                        for p in part['participants']:
                            if not isinstance(p, dict):
                                continue
                            chevaux_out.append(horse_record(iso, rn, cn, p, depart))
                            audit['chevaux'] += 1
                            rank = p.get('ordreArrivee')
                            if isinstance(rank, (int, float)) and rank > 0:
                                rank_rows.append({
                                    'date':iso, 'reunion':rn, 'course':cn,
                                    'numero':p.get('numPmu'), 'nom':p.get('nom'),
                                    'rang':int(rank), 'statut_historique':p.get('statut'),
                                })
                                audit['arrivees_renseignees'] += 1
                        arrivees_out.extend(rank_rows)
                        meta['arrivee_complete_disponible'] = int(all(any(a['rang'] == k for a in rank_rows) for k in range(1, 6)))
                    else:
                        audit['erreurs'] += 1
                        errors.append({'date':iso, 'reunion':rn, 'course':cn,
                                       'etape':'participants', 'message':err or 'Structure JSON participants incorrecte'})

                    payout, payout_url, err = client.read(
                        f'/programme/{pmu}/R{rn}/C{cn}/rapports-definitifs',
                        f'{iso}/R{rn}/C{cn}/rapports-definitifs.json',
                    )
                    if isinstance(payout, list):
                        write_json(race_folder / 'rapports-definitifs.json', payout)
                        meta['rapports_recuperes'] = 1
                        audit['rapports_ok'] += 1
                        for game in payout:
                            if not isinstance(game, dict):
                                continue
                            for report in game.get('rapports', []):
                                if not isinstance(report, dict):
                                    continue
                                rapports_out.append({
                                    'date':iso, 'reunion':rn, 'course':cn,
                                    'type_pari':game.get('typePari'), 'libelle':report.get('libelle'),
                                    'combinaison':report.get('combinaison'),
                                    'mise_base_brute':game.get('miseBase'),
                                    'dividende_brut':report.get('dividende'),
                                    'dividende_pour_un_euro_brut':report.get('dividendePourUnEuro'),
                                    'unite':report.get('dividendeUnite'),
                                })
                    else:
                        audit['erreurs'] += 1
                        errors.append({'date':iso, 'reunion':rn, 'course':cn,
                                       'etape':'rapports-definitifs',
                                       'message':err or 'Structure rapports incorrecte'})
                    courses_out.append(meta)
            day_audit.append(audit)
            print(f"{iso}: réunions={audit['reunions_annoncees']} courses={audit['courses_annoncees']} "
                  f"partants={audit['participants_ok']} rapports={audit['rapports_ok']} "
                  f"erreurs={audit['erreurs']}", flush=True)
    finally:
        client.close()
    for file, fields, values in [
        ('reunions.csv', REUNIONS, reunions_out),
        ('courses.csv', COURSES, courses_out),
        ('chevaux_historique_NON_CERTIFIE_AVANT_COURSE.csv', CHEVAUX, chevaux_out),
        ('arrivees_CIBLES_POST_COURSE.csv', ARRIVEES, arrivees_out),
        ('rapports_DEFINITIFS_POST_COURSE.csv', RAPPORTS, rapports_out),
        ('couverture.csv', COUVERTURE, day_audit),
    ]:
        write_csv(dest / file, fields, values)
    write_json(dest / 'erreurs.json', errors)
    result = {
        'version':'V27', 'collecte_utc':utc_now(), 'periode':[start.isoformat(), end.isoformat()],
        'mode':'verification_sur_archive_locale' if offline_zip else 'API_historique_PMU',
        'pays_filtre': pays_filtre,
        'jours_demandes':len(day_audit),
        'jours_avec_programme':sum(r['programme_ok'] for r in day_audit),
        'reunions':len(reunions_out), 'courses':len(courses_out),
        'courses_avec_partants':sum(c['participants_recuperes'] for c in courses_out),
        'chevaux':len(chevaux_out), 'rangs_arrivee_disponibles':len(arrivees_out),
        'courses_avec_rapports':sum(c['rapports_recuperes'] for c in courses_out),
        'erreurs':len(errors),
        'cotes_pre_course_certifiees':0,
        'avertissement':(
            'La plupart des champs des fiches historiques ne sont PAS certifiés tels que connus avant course. '
            'Les rapports historiques sont définitifs, les résultats sont des cibles. '
            'Les rapports de référence incluent parfois un horodatage antérieur au départ '
            'mais ne prouvent pas à eux seuls un vrai snapshot horodaté complet. '
            'Aucun modèle ne doit être entraîné sans un audit de disponibilité temporelle.'
        ),
    }
    write_json(dest / 'bilan.json', result)
    return result


def main():
    p = argparse.ArgumentParser(description='Collecte PMU par course et par partant (max 7 jours par exécution)')
    p.add_argument('--debut', required=True, help='AAAA-MM-JJ')
    p.add_argument('--fin', required=True, help='AAAA-MM-JJ')
    p.add_argument('--sortie', default='export_pmu')
    p.add_argument('--archive-locale', help='ZIP d\'archive pour test hors réseau')
    p.add_argument('--pays', default='FR', choices=['FR', 'TOUS'],
                   help='Par défaut réunions françaises, toutes disciplines. TOUS pour les autres pays.')
    args = p.parse_args()
    try:
        result = collect(date.fromisoformat(args.debut), date.fromisoformat(args.fin),
                         Path(args.sortie), args.archive_locale, args.pays)
    except (ValueError, OSError, zipfile.BadZipFile) as err:
        print(f'ECHEC: {err}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['jours_avec_programme'] == result['jours_demandes'] else 1


if __name__ == '__main__':
    sys.exit(main())

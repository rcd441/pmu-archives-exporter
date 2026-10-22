/**
 * PMU JSON : correctif du parsing des programmes historiques.
 * Remplace api/scrapers/pmuJsonScraper.js dans PMU Archives Exporter.
 * La disponibilité du flux tiers dépend du fournisseur.
 */
import { MONTH_NAMES_ARRAY } from '../utils/constants.js';

const MAX_DAYS = 31;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function parseIsoDate(value) {
  if (typeof value !== 'string' || !ISO_DATE.test(value)) {
    throw new Error(`Date invalide : ${value}. Format attendu AAAA-MM-JJ.`);
  }
  const date = new Date(`${value}T12:00:00.000Z`);
  if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
    throw new Error(`Date invalide : ${value}`);
  }
  return date;
}

function dateIso(date) {
  return date.toISOString().slice(0, 10);
}

function datePmu(date) {
  return `${String(date.getUTCDate()).padStart(2, '0')}${String(date.getUTCMonth() + 1).padStart(2, '0')}${date.getUTCFullYear()}`;
}

function getDates(years, months, dateFrom, dateTo) {
  const dates = [];
  if (dateFrom && dateTo) {
    const start = parseIsoDate(dateFrom);
    const end = parseIsoDate(dateTo);
    if (start > end) throw new Error('La date de début dépasse la date de fin.');
    for (let d = start; d <= end; d = new Date(d.getTime() + 86400000)) {
      dates.push(d);
      if (dates.length > MAX_DAYS) throw new Error(`Choisis au maximum ${MAX_DAYS} jours par recherche.`);
    }
  } else {
    for (const yearValue of years || []) {
      const year = Number(yearValue);
      if (!Number.isInteger(year) || year < 2000 || year > 2100) continue;
      for (const monthValue of months || []) {
        const monthIndex = MONTH_NAMES_ARRAY.indexOf(String(monthValue).toLowerCase());
        if (monthIndex < 0) continue;
        const lastDay = new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
        for (let day = 1; day <= lastDay; day++) {
          dates.push(new Date(Date.UTC(year, monthIndex, day, 12)));
          if (dates.length > MAX_DAYS) throw new Error(`Choisis au maximum ${MAX_DAYS} jours par recherche.`);
        }
      }
    }
  }
  return dates;
}

/** Transforme les objets réellement présents dans programme.reunions. */
export function normalizePmuReunion(pmuData, date) {
  if (!pmuData || typeof pmuData !== 'object') return null;

  const hip = pmuData.hippodrome ?? pmuData.nomHippodrome ?? pmuData.lieu;
  const hippodrome = typeof hip === 'string'
    ? hip
    : (hip?.libelleCourt ?? hip?.libelleLong ?? hip?.nom ?? 'Inconnu');
  const reunionNumber = String(
    pmuData.numOfficiel ?? pmuData.numExterne ??
    pmuData.reunionNumber ?? pmuData.numeroReunion ?? pmuData.numero ?? 1
  );

  const rawCountry = pmuData.pays ?? pmuData.countryCode ?? 'FR';
  const codePays = typeof rawCountry === 'object'
    ? (rawCountry?.code ?? rawCountry?.libelle ?? 'FR')
    : rawCountry;
  const code = String(codePays).trim().toUpperCase();
  const countries = {
    FRA: 'FR', FRANCE: 'FR', FR: 'FR',
    GBR: 'GB', GB: 'GB',
    DEU: 'GER', GER: 'GER',
    IRL: 'IRE', IRE: 'IRE',
    ITA: 'ITA', SWE: 'SWE', USA: 'USA',
  };
  const countryCode = countries[code] || code;

  const iso = dateIso(date);
  const year = date.getUTCFullYear();
  const month = date.getUTCMonth() + 1;
  const day = date.getUTCDate();
  const name = String(hippodrome);

  return {
    id: `${iso}_${name}_${reunionNumber}`.replace(/[^a-zA-Z0-9_]/g, '_'),
    dateISO: iso,
    dateLabel: `${day}/${month}/${year}`,
    year,
    month,
    monthLabel: new Intl.DateTimeFormat('fr-FR', { month: 'long', timeZone: 'UTC' }).format(date),
    hippodrome: name,
    reunionNumber,
    countryCode,
    url: pmuData.url || pmuData.lien || null,
    source: 'pmu-json',
  };
}

function extractProgrammeReunions(data) {
  if (Array.isArray(data?.programme?.reunions)) return data.programme.reunions;
  if (Array.isArray(data?.reunions)) return data.reunions;
  if (Array.isArray(data)) {
    return data.flatMap((item) => Array.isArray(item?.reunions) ? item.reunions : [item]);
  }
  throw new Error('Réponse PMU non reconnue : le champ programme.reunions est absent.');
}

async function scrapePmuDate(date) {
  const formatted = datePmu(date);
  const url = `https://offline.turfinfo.api.pmu.fr/rest/client/7/programme/${formatted}`;
  let response;
  try {
    response = await fetch(url, { headers: { Accept: 'application/json' } });
  } catch (err) {
    throw new Error(`API PMU inaccessible le ${dateIso(date)} : ${err.message}`);
  }
  if (response.status === 404) return [];
  if (!response.ok) {
    throw new Error(`API PMU : erreur HTTP ${response.status} le ${dateIso(date)}`);
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`Réponse API PMU non JSON le ${dateIso(date)}.`);
  }
  return extractProgrammeReunions(data)
    .map((row) => normalizePmuReunion(row, date))
    .filter(Boolean);
}

export async function scrapePmuJsonArchives(years = [], months = [], dateFrom, dateTo) {
  const dates = getDates(years, months, dateFrom, dateTo);
  if (!dates.length) throw new Error('Choisis une période valide avant de lancer la recherche.');
  const all = [];
  for (let i = 0; i < dates.length; i++) {
    const rows = await scrapePmuDate(dates[i]);
    all.push(...rows);
    if (i + 1 < dates.length) await new Promise((resolve) => setTimeout(resolve, 300));
  }
  const seen = new Set();
  return all.filter((row) => {
    if (seen.has(row.id)) return false;
    seen.add(row.id);
    return true;
  });
}

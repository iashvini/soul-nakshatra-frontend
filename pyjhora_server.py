# pyjhora_server.py - Full Vedic Astrology Flask app for Modal deployment
# Routes: /health, /api/generate-chart-pyjhora, /api/analyze-chart, /api/chat

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import traceback
import os
import json
import urllib.request
import uuid
from datetime import datetime, timedelta, date
from collections import namedtuple
import google.generativeai as genai

# ── AI Client Setup ──
# Switch between xAI and Gemini by changing USE_XAI flag
USE_XAI = True  # Set to False to revert to Gemini

xai_client = None
try:
    from openai import OpenAI as _OpenAI
    xai_client = _OpenAI(
        api_key=os.environ.get('GROK_API_KEY', ''),
        base_url='https://api.x.ai/v1'
    )
    print('✓ xAI Grok client initialized')
except Exception as _e:
    print(f'✗ xAI client failed: {_e}')

app = Flask(__name__)
CORS(app)

PYJHORA_AVAILABLE = False
PYJHORA_IMPORT_ERROR = None

try:
    from jhora import const, utils
    from jhora.panchanga import drik
    from jhora.horoscope.chart import charts, ashtakavarga as av
    import swisseph as swe
    PYJHORA_AVAILABLE = True
    print("✓ PyJHora and Swiss Ephemeris successfully imported!")
except Exception as e:
    PYJHORA_IMPORT_ERROR = str(e)
    print(f"✗ PyJHora import failed: {e}")


# --------------------------------------------------
# Session store (in-memory, per-container)
# --------------------------------------------------
session_store = {}


# --------------------------------------------------
# Notion logger
# --------------------------------------------------
def log_to_notion(sess):
    import traceback as _tb
    token       = os.environ.get("NOTION_TOKEN", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")

    session_id = sess.get("session_id", "unknown")
    print(f"[Notion] Starting log for session {session_id}")
    print(f"[Notion] Token present: {bool(token)} | DB ID present: {bool(database_id)}")

    if not token or not database_id:
        print("[Notion] MISSING CREDENTIALS — cannot log")
        return

    questions_list = sess.get("questions", [])
    questions_str  = "\n".join(questions_list) if questions_list else "None"
    if len(questions_str) > 1900:
        questions_str = questions_str[:1900] + "\n...(truncated)"

    print(f"[Notion] Questions to log: {len(questions_list)}")
    print(f"[Notion] Chart generated: {sess.get('chart_generated')}")

    timestamp = sess.get("timestamp", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"))

    payload = json.dumps({
        "parent": {"database_id": database_id},
        "properties": {
            "Timestamp":         {"title": [{"text": {"content": str(timestamp)}}]},
            "Session ID":        {"rich_text": [{"text": {"content": str(session_id)}}]},
            "Birth Date":        {"rich_text": [{"text": {"content": str(sess.get("birth_date", ""))}}]},
            "Birth Time":        {"rich_text": [{"text": {"content": str(sess.get("birth_time", ""))}}]},
            "Birth Place":       {"rich_text": [{"text": {"content": str(sess.get("birth_place", ""))}}]},
            "Chart Generated":   {"checkbox": bool(sess.get("chart_generated", False))},
            "Analysis provided": {"checkbox": bool(sess.get("analysis_provided", False))},
            "Questions Asked":   {"rich_text": [{"text": {"content": questions_str}}]},
            "Error log":         {"rich_text": [{"text": {"content": str(sess.get("error_log", ""))[:200]}}]},
            "Source":            {"rich_text": [{"text": {"content": "Cloudflare"}}]},
        }
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=payload,
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
            "Notion-Version": "2022-06-28",
        },
        method="POST",
    )
    try:
        print("[Notion] Sending request to Notion API...")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[Notion] Success! Response status: {resp.status}")
            print(f"[Notion] Logged session {session_id}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"[Notion] HTTP Error {e.code}: {body}")
    except urllib.error.URLError as e:
        print(f"[Notion] URL Error: {e.reason}")
    except Exception as e:
        print(f"[Notion] Unexpected error: {e}")
        _tb.print_exc()


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def compute_julian_day(year, month, day, hour, minute, tz_offset_hours):
    utc_hour = hour + minute / 60.0 - tz_offset_hours
    jd = swe.julday(year, month, day, utc_hour)
    return jd


def compute_sidereal_ascendant(jd, latitude, longitude):
    cusps, ascmc = swe.houses(jd, latitude, longitude)
    tropical_asc = ascmc[0]
    ayan = swe.get_ayanamsa(jd)
    sidereal_asc = (tropical_asc - ayan) % 360.0
    return sidereal_asc


def compute_sidereal_planet_longitude(jd, planet_id):
    planet_pos = swe.calc_ut(jd, planet_id)[0]
    tropical_lon = planet_pos[0]
    speed = planet_pos[3]
    ayan = swe.get_ayanamsa(jd)
    sidereal_lon = (tropical_lon - ayan) % 360.0
    return sidereal_lon, speed


def calculate_nakshatra_info(lon_deg):
    nakshatra_names = [
        'Ashwini', 'Bharani', 'Krittika', 'Rohini', 'Mrigashira', 'Ardra',
        'Punarvasu', 'Pushya', 'Ashlesha', 'Magha', 'Purva Phalguni', 'Uttara Phalguni',
        'Hasta', 'Chitra', 'Swati', 'Vishakha', 'Anuradha', 'Jyeshtha',
        'Moola', 'Purva Ashadha', 'Uttara Ashadha', 'Shravana', 'Dhanishta',
        'Shatabhisha', 'Purva Bhadrapada', 'Uttara Bhadrapada', 'Revati'
    ]
    one_nak = 360.0 / 27.0
    nak_id = int((lon_deg % 360.0) / one_nak)
    nak_name = nakshatra_names[nak_id]
    nak_deg_within = lon_deg % one_nak
    pada = int(nak_deg_within / (one_nak / 4.0)) + 1
    return nak_id, nak_name, pada


# --------------------------------------------------
# Antardasha calculator
# --------------------------------------------------

def compute_antardashas(maha_lord, maha_start_dt, maha_full_years, elapsed_years_before_start=0.0):
    lords = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
    durations = {
        'Ketu': 7, 'Venus': 20, 'Sun': 6, 'Moon': 10, 'Mars': 7,
        'Rahu': 18, 'Jupiter': 16, 'Saturn': 19, 'Mercury': 17,
    }
    start_idx = lords.index(maha_lord)
    virtual_md_start = maha_start_dt - timedelta(days=elapsed_years_before_start * 365.25)
    actual_md_end = maha_start_dt + timedelta(
        days=(maha_full_years - elapsed_years_before_start) * 365.25
    )
    result = []
    cursor = virtual_md_start
    for i in range(9):
        idx = (start_idx + i) % 9
        antar_lord = lords[idx]
        ad_years = (maha_full_years * durations[antar_lord]) / 120.0
        ad_end = cursor + timedelta(days=ad_years * 365.25)
        real_start = max(cursor, maha_start_dt)
        real_end = min(ad_end, actual_md_end)
        if real_end > real_start:
            result.append({
                'planet':         antar_lord,
                'start':          real_start.date().isoformat(),
                'end':            real_end.date().isoformat(),
                'duration_years': round((real_end - real_start).days / 365.25, 3),
            })
        cursor = ad_end
        if cursor >= actual_md_end:
            break
    return result


# --------------------------------------------------
# Pratyantar Dasha calculator
# --------------------------------------------------

def compute_pratyantardashas(maha_lord, antar_lord, antar_start_dt, antar_duration_years):
    """
    Compute Pratyantar Dashas within a given Antardasha period.
    Formula: PD_years = (antar_duration_years * PD_full_years) / 120
    Sequence starts from the Antardasha lord.
    """
    lords = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
    durations = {
        'Ketu': 7, 'Venus': 20, 'Sun': 6, 'Moon': 10, 'Mars': 7,
        'Rahu': 18, 'Jupiter': 16, 'Saturn': 19, 'Mercury': 17,
    }
    start_idx = lords.index(antar_lord)
    antar_end_dt = antar_start_dt + timedelta(days=antar_duration_years * 365.25)
    cursor = antar_start_dt
    result = []
    for i in range(9):
        idx = (start_idx + i) % 9
        pd_lord = lords[idx]
        pd_years = (antar_duration_years * durations[pd_lord]) / 120.0
        pd_end = cursor + timedelta(days=pd_years * 365.25)
        real_end = min(pd_end, antar_end_dt)
        if real_end > cursor:
            result.append({
                'planet': pd_lord,
                'start': cursor.date().isoformat(),
                'end': real_end.date().isoformat(),
                'duration_days': round((real_end - cursor).days, 1),
            })
        cursor = pd_end
        if cursor >= antar_end_dt:
            break
    return result


# --------------------------------------------------
# Vimshottari Dasha
# --------------------------------------------------

def compute_vimshottari_dasha(moon_data, birth_dt):
    moon_lon = moon_data['longitude']
    nak_size = 360.0 / 27.0
    nak_id = int((moon_lon % 360.0) / nak_size)
    dasha_lords = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
    dasha_durations_years = {
        'Ketu': 7, 'Venus': 20, 'Sun': 6, 'Moon': 10, 'Mars': 7,
        'Rahu': 18, 'Jupiter': 16, 'Saturn': 19, 'Mercury': 17,
    }
    start_index = nak_id % 9
    start_lord = dasha_lords[start_index]
    pos_in_nak = moon_lon % nak_size
    frac_done = pos_in_nak / nak_size
    frac_left = 1.0 - frac_done
    first_total_years = dasha_durations_years[start_lord]
    first_remaining_years = first_total_years * frac_left
    dasha_cycle = []
    current_start = birth_dt
    for i in range(9):
        idx = (start_index + i) % 9
        lord = dasha_lords[idx]
        duration_years = first_remaining_years if i == 0 else dasha_durations_years[lord]
        duration_days = duration_years * 365.25
        period_end = current_start + timedelta(days=duration_days)
        dasha_cycle.append({
            'planet': lord,
            'start': current_start.date().isoformat(),
            'end': period_end.date().isoformat(),
            'duration_years': duration_years,
        })
        current_start = period_end

    now = datetime.utcnow()
    current_dasha = None
    pratyantar_dashas = []
    for period in dasha_cycle:
        s = datetime.fromisoformat(period['start'])
        e = datetime.fromisoformat(period['end'])
        if s <= now < e:
            maha_full_yrs = dasha_durations_years[period['planet']]
            actual_md_yrs = (e - s).days / 365.25
            elapsed_before = max(0.0, maha_full_yrs - actual_md_yrs)
            antar_dashas = compute_antardashas(
                maha_lord=period['planet'],
                maha_start_dt=s,
                maha_full_years=maha_full_yrs,
                elapsed_years_before_start=elapsed_before,
            )
            current_dasha = {
                'system': 'Vimshottari',
                'reference': 'Moon nakshatra (Lahiri sidereal, approximate durations)',
                'birth_nakshatra': {
                    'id': nak_id,
                    'name': moon_data['nakshatra']['name'],
                    'pada': moon_data['nakshatra']['pada'],
                },
                'maha_dasha': {
                    'planet': period['planet'],
                    'start_date': period['start'],
                    'end_date': period['end'],
                    'duration_years': maha_full_yrs,
                },
                'antar_dashas': antar_dashas,
                'all_mahadashas': dasha_cycle,
            }
            # Find active antardasha and compute pratyantar dashas
            for ad in antar_dashas:
                try:
                    ad_s = datetime.fromisoformat(ad['start'])
                    ad_e = datetime.fromisoformat(ad['end'])
                    if ad_s <= now <= ad_e:
                        pratyantar_dashas = compute_pratyantardashas(
                            period['planet'], ad['planet'], ad_s, ad['duration_years']
                        )
                        for pd in pratyantar_dashas:
                            pd_s = datetime.fromisoformat(pd['start'])
                            pd_e = datetime.fromisoformat(pd['end'])
                            if pd_s <= now <= pd_e:
                                current_dasha['pratyantar'] = pd['planet']
                                current_dasha['pratyantar_start'] = pd['start']
                                current_dasha['pratyantar_end'] = pd['end']
                                current_dasha['pratyantar_duration_days'] = pd['duration_days']
                                break
                        break
                except Exception:
                    continue
            break

    if not current_dasha and dasha_cycle:
        last = dasha_cycle[-1]
        current_dasha = {
            'system': 'Vimshottari',
            'reference': 'Moon nakshatra (Lahiri sidereal, approximate durations)',
            'birth_nakshatra': {
                'id': nak_id,
                'name': moon_data['nakshatra']['name'],
                'pada': moon_data['nakshatra']['pada'],
            },
            'maha_dasha': {
                'planet': last['planet'],
                'start_date': last['start'],
                'end_date': last['end'],
                'duration_years': dasha_durations_years[last['planet']],
            },
            'all_mahadashas': dasha_cycle,
        }
    return current_dasha, dasha_cycle, pratyantar_dashas


def calculate_dasha_simple(moon_longitude):
    nak_size = 360.0 / 27.0
    nak_id = int((moon_longitude % 360.0) / nak_size)
    dasha_lords = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
    starting_lord = dasha_lords[nak_id % 9]
    dasha_durations = {
        'Ketu': 7, 'Venus': 20, 'Sun': 6, 'Moon': 10, 'Mars': 7,
        'Rahu': 18, 'Jupiter': 16, 'Saturn': 19, 'Mercury': 17
    }
    return {
        'system': 'Vimshottari',
        'maha_dasha': {
            'planet': starting_lord,
            'duration_years': dasha_durations[starting_lord]
        },
        'note': 'Fallback dasha based on birth Moon nakshatra only.',
    }


# --------------------------------------------------
# Approximate Ashtakavarga fallback
# --------------------------------------------------

def compute_approx_ashtakavarga(planet_positions):
    bindus = []
    for i in range(12):
        house_num = i + 1
        planets_count = sum(1 for p in planet_positions if p['house'] == house_num)
        points = 25 + planets_count * 3
        if house_num in (1, 4, 7, 10):
            points += 3
        elif house_num in (3, 6, 11):
            points += 2
        elif house_num in (8, 12):
            points -= 3
        points = max(min(points, 40), 15)
        bindus.append({'house': house_num, 'points': points})
    return bindus


def calculate_current_transits(birth_asc_long, birth_planets):
    now = datetime.utcnow()
    current_jd = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60.0)
    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    transit_planets = []
    major_planets = [('Jupiter', swe.JUPITER), ('Saturn', swe.SATURN)]
    rasi_names = [
        'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
        'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces'
    ]
    birth_asc_sign = int(birth_asc_long / 30.0)
    for name, swe_id in major_planets:
        lon_sid, speed = compute_sidereal_planet_longitude(current_jd, swe_id)
        rasi_num = int(lon_sid / 30.0)
        nak_id, nak_name, nak_pada = calculate_nakshatra_info(lon_sid)
        planet_sign = int(lon_sid / 30.0)
        house_num = ((planet_sign - birth_asc_sign) % 12) + 1
        transit_planets.append({
            'name': name, 'longitude': round(lon_sid, 2), 'rasi': rasi_names[rasi_num],
            'house': house_num, 'nakshatra': nak_name, 'is_retrograde': speed < 0
        })
    rahu_lon_sid, _ = compute_sidereal_planet_longitude(current_jd, swe.MEAN_NODE)
    ketu_lon_sid = (rahu_lon_sid + 180.0) % 360.0
    for name, lon_sid in [('Rahu', rahu_lon_sid), ('Ketu', ketu_lon_sid)]:
        rasi_num = int(lon_sid / 30.0)
        nak_id, nak_name, nak_pada = calculate_nakshatra_info(lon_sid)
        planet_sign = int(lon_sid / 30.0)
        house_num = ((planet_sign - birth_asc_sign) % 12) + 1
        transit_planets.append({
            'name': name, 'longitude': round(lon_sid, 2), 'rasi': rasi_names[rasi_num],
            'house': house_num, 'nakshatra': nak_name, 'is_retrograde': True
        })
    aspects = []
    for t_planet in transit_planets:
        for n_planet in birth_planets:
            diff = abs(t_planet['longitude'] - n_planet['longitude'])
            if diff > 180:
                diff = 360 - diff
            if diff <= 10:
                aspects.append({'type': 'Conjunction', 'transit_planet': t_planet['name'],
                                 'natal_planet': n_planet['name'], 'orb': round(diff, 1), 'significance': 'High'})
            elif abs(diff - 180) <= 10:
                aspects.append({'type': 'Opposition', 'transit_planet': t_planet['name'],
                                 'natal_planet': n_planet['name'], 'orb': round(abs(diff - 180), 1), 'significance': 'High'})
    return {
        'current_date': now.strftime('%Y-%m-%d'),
        'planets': transit_planets,
        'major_aspects': aspects,
        'note': 'Only major slow-moving planets tracked: Jupiter, Saturn, Rahu, Ketu'
    }


def get_sign_lord(sign_num):
    lords = ['Mars', 'Venus', 'Mercury', 'Moon', 'Sun', 'Mercury',
             'Venus', 'Mars', 'Jupiter', 'Saturn', 'Saturn', 'Jupiter']
    return lords[sign_num % 12]


def calculate_divisional_charts(jd, birth_planets):
    rasi_names = [
        'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
        'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces'
    ]
    divisional_charts = {
        'D9': {'name': 'Navamsa', 'planets': []},
        'D10': {'name': 'Dasamsa', 'planets': []}
    }
    for planet in birth_planets:
        planet_lon = planet['longitude']
        planet_name = planet['name']
        sign = int(planet_lon / 30.0)
        degree_in_sign = planet_lon % 30.0
        navamsa_num = int(degree_in_sign / (30.0 / 9.0))
        sign_element = sign % 4
        if sign_element == 0:
            navamsa_sign = (0 + navamsa_num) % 12
        elif sign_element == 1:
            navamsa_sign = (9 + navamsa_num) % 12
        elif sign_element == 2:
            navamsa_sign = (6 + navamsa_num) % 12
        else:
            navamsa_sign = (3 + navamsa_num) % 12
        divisional_charts['D9']['planets'].append({
            'name': planet_name,
            'sign': rasi_names[navamsa_sign],
            'sign_lord': get_sign_lord(navamsa_sign),
            'debug': f"Birth: {rasi_names[sign]} {degree_in_sign:.2f}° → Nav #{navamsa_num+1} → {rasi_names[navamsa_sign]}"
        })
        dasamsa_number = int(degree_in_sign / 3.0)
        if sign % 2 == 0:
            dasamsa_sign = (sign + dasamsa_number) % 12
        else:
            dasamsa_sign = (sign + 8 + dasamsa_number) % 12
        divisional_charts['D10']['planets'].append({
            'name': planet_name,
            'sign': rasi_names[dasamsa_sign],
            'sign_lord': get_sign_lord(dasamsa_sign)
        })
    return divisional_charts


def analyze_career_from_d10(d10_chart, d1_planets, houses=None, ashtakavarga_bindus=None):
    sun_d10     = next((p for p in d10_chart['planets'] if p['name'] == 'Sun'),     None)
    jupiter_d10 = next((p for p in d10_chart['planets'] if p['name'] == 'Jupiter'), None)
    saturn_d10  = next((p for p in d10_chart['planets'] if p['name'] == 'Saturn'),  None)
    mercury_d10 = next((p for p in d10_chart['planets'] if p['name'] == 'Mercury'), None)

    career_indicators = {
        'primary_field': None,
        'secondary_fields': [],
        'professional_strengths': [],
        'it_indicators': [],
        'career_nature': None
    }

    # ── IT / Technology check (always first) ──
    it_indicators = []
    for planet in d1_planets:
        pname = planet.get('name', '')
        rasi_name = (planet.get('rasi') or {}).get('name', '')
        if pname == 'Mercury':
            if planet.get('house') == 10:
                it_indicators.append('Mercury in 10th house — strong IT and analytical aptitude')
            if rasi_name == 'Gemini':
                it_indicators.append('Mercury in own sign (Gemini) — technical and logical mind')
            if rasi_name == 'Virgo':
                it_indicators.append('Mercury exalted in Virgo — exceptional analytical ability')
        if pname == 'Rahu' and planet.get('house') == 10:
            it_indicators.append('Rahu in 10th house — strong pull toward technology and IT careers')

    if houses and len(houses) >= 10:
        tenth_sign = (houses[9].get('rasi') or {}).get('name', '')
        if tenth_sign in ['Gemini', 'Aquarius', 'Virgo']:
            it_indicators.append(f'{tenth_sign} on 10th house cusp — technology and innovation oriented career')

    if ashtakavarga_bindus:
        for h in ashtakavarga_bindus:
            if h.get('house') == 10 and h.get('points', 0) >= 30:
                it_indicators.append('Strong 10th house strength score — career success well supported')

    if it_indicators:
        career_indicators['professional_strengths'].insert(0, 'IT / Technology / Software — ' + '; '.join(it_indicators))
        career_indicators['it_indicators'] = it_indicators

    # ── Standard D10 indicators ──
    if sun_d10:
        lord = sun_d10['sign_lord']
        if lord in ['Sun', 'Mars']:
            career_indicators['professional_strengths'].append('Leadership roles, government positions, authority')
        elif lord == 'Jupiter':
            career_indicators['professional_strengths'].append('Teaching, advisory, consulting, management')
        elif lord == 'Saturn':
            career_indicators['professional_strengths'].append('Administration, long-term projects, systematic work')
    if mercury_d10:
        career_indicators['professional_strengths'].append('Communication, business, trading, intellectual work')
    if jupiter_d10:
        career_indicators['professional_strengths'].append('Finance, education, counseling, advisory roles')
    if saturn_d10:
        career_indicators['professional_strengths'].append('Technical fields, service sector, disciplined professions')
    return career_indicators


# --------------------------------------------------
# Core chart calculation
# --------------------------------------------------

def calculate_chart(name, birth_dt, latitude, longitude, tz_offset_hours, jd):
    lat = float(latitude)
    lon = float(longitude)
    asc_long = compute_sidereal_ascendant(jd, lat, lon)
    rasi_names = [
        'Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo',
        'Libra', 'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces'
    ]
    asc_rasi_num = int(asc_long / 30.0)
    asc_nak_id, asc_nak_name, asc_pada = calculate_nakshatra_info(asc_long)
    ascendant = {
        'degree': round(asc_long, 6),
        'rasi': {'id': asc_rasi_num, 'name': rasi_names[asc_rasi_num]},
        'nakshatra': {'id': asc_nak_id, 'name': asc_nak_name, 'pada': asc_pada}
    }
    planet_names = ['Sun', 'Moon', 'Mars', 'Mercury', 'Jupiter', 'Venus', 'Saturn', 'Rahu', 'Ketu']
    planet_swe_ids = [swe.SUN, swe.MOON, swe.MARS, swe.MERCURY, swe.JUPITER, swe.VENUS, swe.SATURN]
    planet_positions = []
    swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
    asc_sign = int(asc_long / 30.0)
    for i, swe_id in enumerate(planet_swe_ids):
        lon_sid, speed = compute_sidereal_planet_longitude(jd, swe_id)
        rasi_num = int(lon_sid / 30.0)
        nak_id, nak_name, nak_pada = calculate_nakshatra_info(lon_sid)
        degree_in_sign = lon_sid % 30.0
        d_int = int(degree_in_sign)
        minute = int((degree_in_sign - d_int) * 60.0)
        second = int((((degree_in_sign - d_int) * 60.0) - minute) * 60.0)
        planet_sign = int(lon_sid / 30.0)
        house_num = ((planet_sign - asc_sign) % 12) + 1
        planet_positions.append({
            'id': i, 'name': planet_names[i], 'longitude': round(lon_sid, 6),
            'house': house_num,
            'rasi': {'id': rasi_num, 'name': rasi_names[rasi_num]},
            'nakshatra': {'id': nak_id, 'name': nak_name, 'pada': nak_pada},
            'position': {'degree': d_int, 'minute': minute, 'second': second},
            'is_retrograde': speed < 0
        })
    rahu_lon_sid, _ = compute_sidereal_planet_longitude(jd, swe.MEAN_NODE)
    ketu_lon_sid = (rahu_lon_sid + 180.0) % 360.0
    for idx, lon_sid in enumerate([rahu_lon_sid, ketu_lon_sid], start=7):
        rasi_num = int(lon_sid / 30.0)
        nak_id, nak_name, nak_pada = calculate_nakshatra_info(lon_sid)
        degree_in_sign = lon_sid % 30.0
        d_int = int(degree_in_sign)
        minute = int((degree_in_sign - d_int) * 60.0)
        second = int((((degree_in_sign - d_int) * 60.0) - minute) * 60.0)
        planet_sign = int(lon_sid / 30.0)
        house_num = ((planet_sign - asc_sign) % 12) + 1
        planet_positions.append({
            'id': idx, 'name': planet_names[idx], 'longitude': round(lon_sid, 6),
            'house': house_num,
            'rasi': {'id': rasi_num, 'name': rasi_names[rasi_num]},
            'nakshatra': {'id': nak_id, 'name': nak_name, 'pada': nak_pada},
            'position': {'degree': d_int, 'minute': minute, 'second': second},
            'is_retrograde': False
        })
    moon_data = planet_positions[1]
    sun_data = planet_positions[0]
    nakshatra_lords = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
    moon_nak_lord = nakshatra_lords[moon_data['nakshatra']['id'] % 9]
    nakshatra_details = {
        'nakshatra': {
            'id': moon_data['nakshatra']['id'],
            'name': moon_data['nakshatra']['name'],
            'pada': moon_data['nakshatra']['pada'],
            'lord': {'name': moon_nak_lord}
        },
        'chandra_rasi': moon_data['rasi'],
        'soorya_rasi': sun_data['rasi']
    }
    houses = []
    for i in range(12):
        house_sign = (asc_sign + i) % 12
        house_cusp = house_sign * 30.0
        planets_in_house = [p['id'] for p in planet_positions if p['house'] == i + 1]
        suffix = "st" if i == 0 else "nd" if i == 1 else "rd" if i == 2 else "th"
        houses.append({
            'id': i + 1, 'name': f'{i + 1}{suffix} House',
            'cusp': round(house_cusp, 6),
            'rasi': {'id': house_sign, 'name': rasi_names[house_sign], 'lord': {'name': get_sign_lord(house_sign)}},
            'planets_in_house': planets_in_house
        })
    pratyantar_dashas = []
    try:
        current_dasha, dasha_cycle, pratyantar_dashas = compute_vimshottari_dasha(moon_data, birth_dt)
    except Exception as e:
        print("Dasha calculation error:", e)
        traceback.print_exc()
        current_dasha, dasha_cycle = None, None
    if not current_dasha:
        current_dasha = calculate_dasha_simple(moon_data['longitude'])
        dasha_cycle = None
    ashtakavarga_bindus = None
    try:
        print("\n[Ashtakavarga] Starting computation...")
        pyjhora_planet_list = []
        lagna_rasi = int(asc_long / 30.0)
        lagna_deg = asc_long % 30.0
        pyjhora_planet_list.append(['L', (lagna_rasi, lagna_deg)])
        for p in planet_positions:
            planet_rasi = p['rasi']['id']
            planet_deg = p['longitude'] % 30.0
            pyjhora_planet_list.append([p['id'], (planet_rasi, planet_deg)])
        chart_1d = utils.get_house_planet_list_from_planet_positions(pyjhora_planet_list)
        binna, sarva, prastara = av.get_ashtaka_varga(chart_1d)
        ashtakavarga_bindus = []
        for house_num in range(1, 13):
            rasi_index = (asc_sign + house_num - 1) % 12
            points = int(sarva[rasi_index])
            ashtakavarga_bindus.append({"house": house_num, "rasi": rasi_names[rasi_index], "points": points})
    except Exception as e:
        print(f"[Ashtakavarga] ERROR: {e}")
        ashtakavarga_bindus = None
    if ashtakavarga_bindus is None:
        ashtakavarga_bindus = compute_approx_ashtakavarga(planet_positions)
    if ashtakavarga_bindus:
        ashtakavarga_bindus = [{"house": item["house"], "points": item["points"]} for item in ashtakavarga_bindus]
    try:
        transits = calculate_current_transits(asc_long, planet_positions)
        print('[TRANSIT DEBUG] Full transits object:', json.dumps(transits, default=str))
    except Exception as e:
        print(f"[Transits] ERROR: {e}")
        transits = None
    # ── Battery Drain diagnostic ──
    try:
        _rasi_names = ['Aries','Taurus','Gemini','Cancer','Leo','Virgo',
                       'Libra','Scorpio','Sagittarius','Capricorn','Aquarius','Pisces']
        twelfth_sign = _rasi_names[(int(asc_long / 30.0) + 11) % 12]
        natal_12th = [p['name'] for p in planet_positions if p.get('house') == 12]
        transiting_12th = [
            p['name'] for p in ((transits or {}).get('planets') or [])
            if p.get('house') == 12
        ]
        _md_planet = (current_dasha or {}).get('maha_dasha', {}).get('planet', '')
        dasha_lord_in_12th = _md_planet in transiting_12th if _md_planet else False
        print(f'[Battery Drain] 12th sign: {twelfth_sign}')
        print(f'[Battery Drain] Natal planets in 12th: {natal_12th}')
        print(f'[Battery Drain] Transiting 12th: {transiting_12th}')
        print(f'[Battery Drain] Dasha lord in 12th: {dasha_lord_in_12th} (lord={_md_planet})')
    except Exception as e:
        print(f'[Battery Drain] ERROR: {e}')
    try:
        divisional_charts = calculate_divisional_charts(jd, planet_positions)
        print('[D9 DEBUG]', json.dumps(divisional_charts.get('D9', {}), default=str)[:500])
        career_analysis = analyze_career_from_d10(divisional_charts['D10'], planet_positions, houses=houses, ashtakavarga_bindus=ashtakavarga_bindus)
    except Exception as e:
        print(f"[Divisional Charts] ERROR: {e}")
        divisional_charts = None
        career_analysis = None
    property_transit_history = calculate_property_transit_history(houses, years_back=7)
    moon_sign_str = nakshatra_details.get('chandra_rasi', {}).get('name', '') if nakshatra_details else ''
    stress_transit_history = calculate_stress_transit_history(houses, moon_sign_str, years_back=7)

    return {
        'name': name,
        'data': {
            'ascendant': ascendant,
            'nakshatra_details': nakshatra_details,
            'planets': planet_positions,
            'houses': houses,
            'dasha': {'current_dasha': current_dasha, 'cycle': dasha_cycle, 'pratyantar_dashas': pratyantar_dashas},
            'ashtakavarga': {'sarvashtakavarga': {'house_wise': ashtakavarga_bindus}},
            'transits': transits,
            'divisional_charts': divisional_charts,
            'career_analysis': career_analysis,
            'property_transit_history': property_transit_history,
            'stress_transit_history':   stress_transit_history,
        },
        'source': 'Swiss Ephemeris (Lahiri sidereal) + PyJHora + Divisional Charts',
        'ayanamsa': 'Lahiri'
    }


# --------------------------------------------------
# Vedic Graha Drishti aspect helpers
# --------------------------------------------------

_SIGNS = ['Aries','Taurus','Gemini','Cancer','Leo','Virgo',
          'Libra','Scorpio','Sagittarius','Capricorn','Aquarius','Pisces']

# Nakshatra → ruling planet lookup (27 nakshatras, Vimshottari order)
NAKSHATRA_LORDS = {
    'Ashwini': 'Ketu',    'Bharani': 'Venus',   'Krittika': 'Sun',
    'Rohini':  'Moon',    'Mrigashira': 'Mars',  'Ardra': 'Rahu',
    'Punarvasu': 'Jupiter', 'Pushya': 'Saturn',  'Ashlesha': 'Mercury',
    'Magha':   'Ketu',    'Purva Phalguni': 'Venus', 'Uttara Phalguni': 'Sun',
    'Hasta':   'Moon',    'Chitra': 'Mars',      'Swati': 'Rahu',
    'Vishakha': 'Jupiter', 'Anuradha': 'Saturn', 'Jyeshtha': 'Mercury',
    'Mula':    'Ketu',    'Purva Ashadha': 'Venus', 'Uttara Ashadha': 'Sun',
    'Shravana': 'Moon',   'Dhanishta': 'Mars',   'Shatabhisha': 'Rahu',
    'Purva Bhadrapada': 'Jupiter', 'Uttara Bhadrapada': 'Saturn', 'Revati': 'Mercury',
}

# Rahu/Ketu transit windows — mean node, Lahiri sidereal
# Format: (start_date, end_date, rahu_sign, ketu_sign)
# UPDATE this table when a new transit begins — everything else is automatic
RAHU_TRANSITS = [
    ('2022-04-12', '2023-10-30', 'Aries',     'Libra'),
    ('2023-10-30', '2025-05-18', 'Pisces',    'Virgo'),
    ('2025-05-18', '2026-12-05', 'Aquarius',  'Leo'),
    ('2026-12-05', '2028-05-26', 'Capricorn', 'Cancer'),
]

_SPECIAL_OFFSETS = {
    'Jupiter': [5, 7, 9],
    'Saturn':  [3, 7, 10],
    'Mars':    [4, 7, 8],
    'Sun':     [7], 'Moon':    [7], 'Mercury': [7],
    'Venus':   [7], 'Rahu':    [7], 'Ketu':    [7],
}

_ASPECT_NAMES = {
    ('Jupiter', 5): 'Jupiter 5th aspect (wisdom and opportunity)',
    ('Jupiter', 7): 'Jupiter 7th aspect (blessings and expansion)',
    ('Jupiter', 9): 'Jupiter 9th aspect (fortune and grace)',
    ('Saturn',  3): 'Saturn 3rd aspect (effort and restriction)',
    ('Saturn',  7): 'Saturn 7th aspect (delays and discipline)',
    ('Saturn', 10): 'Saturn 10th aspect (career pressure and karma)',
    ('Mars',    4): 'Mars 4th aspect (aggression on home/property)',
    ('Mars',    7): 'Mars 7th aspect (conflict in partnerships)',
    ('Mars',    8): 'Mars 8th aspect (sudden events and transformation)',
}

def get_planet_aspects(planet_name, planet_house):
    """Vedic Graha Drishti — returns list of aspected house numbers."""
    if not planet_house:
        return []
    offsets = _SPECIAL_OFFSETS.get(planet_name, [7])
    return list(set(((planet_house - 1 + off - 1) % 12) + 1 for off in offsets))

def describe_aspect(planet_name, from_house, to_house):
    offset = ((to_house - from_house) % 12) + 1
    return _ASPECT_NAMES.get((planet_name, offset),
           f'{planet_name} aspects House {to_house}')

def _get_sign_and_house(jd, ayanamsa, planet_id, birth_houses):
    """Shared helper: sidereal sign + house number for a planet at given JD."""
    import swisseph as swe
    pos = swe.calc_ut(jd, planet_id)[0]
    sidereal = (pos[0] - ayanamsa) % 360
    sign = _SIGNS[int(sidereal / 30)]
    house = None
    if birth_houses:
        for i, h in enumerate(birth_houses):
            if h.get('rasi', {}).get('name', '') == sign:
                house = i + 1
                break
    return sign, house


# --------------------------------------------------
# Property transit history — Saturn, Jupiter, Mars last N years
# --------------------------------------------------

def calculate_property_transit_history(birth_houses, years_back=7):
    """Track Saturn, Jupiter, Mars transits + full Vedic aspects on 4th house."""
    try:
        import swisseph as swe
        today = datetime.utcnow()
        results = []

        for y in range(years_back, -1, -1):
            cd = today.replace(year=today.year - y, month=6, day=15)
            jd = swe.julday(cd.year, cd.month, cd.day, 12.0)
            swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
            ayanamsa = swe.get_ayanamsa_ut(jd)

            sat_sign,  sat_h  = _get_sign_and_house(jd, ayanamsa, swe.SATURN,  birth_houses)
            jup_sign,  jup_h  = _get_sign_and_house(jd, ayanamsa, swe.JUPITER, birth_houses)
            mars_sign, mars_h = _get_sign_and_house(jd, ayanamsa, swe.MARS,    birth_houses)
            rahu_sign, rahu_h = _get_sign_and_house(jd, ayanamsa, swe.MEAN_NODE, birth_houses)
            ketu_h = ((rahu_h - 1 + 6) % 12) + 1 if rahu_h else None
            ketu_sign = _SIGNS[(_SIGNS.index(rahu_sign) + 6) % 12] if rahu_sign in _SIGNS else None

            sat_asp  = get_planet_aspects('Saturn',  sat_h)
            jup_asp  = get_planet_aspects('Jupiter', jup_h)
            mars_asp = get_planet_aspects('Mars',    mars_h)

            activations = []
            if sat_h == 4:
                activations.append('Saturn IN 4th — strongest property trigger: purchase, sale, or major home change')
            elif 4 in sat_asp:
                activations.append(f'Saturn aspects 4th ({describe_aspect("Saturn", sat_h, 4)} from H{sat_h}) — property pressure/delay')
            if jup_h == 4:
                activations.append('Jupiter IN 4th — property opportunity and expansion')
            elif 4 in jup_asp:
                activations.append(f'Jupiter aspects 4th ({describe_aspect("Jupiter", jup_h, 4)} from H{jup_h}) — favorable property blessing')
            if mars_h == 4:
                activations.append('Mars IN 4th — property action, renovation, or dispute')
            elif 4 in mars_asp:
                activations.append(f'Mars aspects 4th ({describe_aspect("Mars", mars_h, 4)} from H{mars_h}) — urgency or conflict in property')
            if rahu_h == 4:
                activations.append('Rahu IN 4th — unusual or foreign property event')
            if ketu_h == 4:
                activations.append('Ketu IN 4th — property loss, detachment, or ancestral home')

            results.append({
                'year':               cd.year,
                'saturn_sign':        sat_sign,  'saturn_house':  sat_h,
                'saturn_aspects':     sat_asp,   'saturn_aspects_4th': 4 in sat_asp,
                'jupiter_sign':       jup_sign,  'jupiter_house': jup_h,
                'jupiter_aspects':    jup_asp,   'jupiter_aspects_4th': 4 in jup_asp,
                'mars_sign':          mars_sign, 'mars_house':    mars_h,
                'mars_aspects':       mars_asp,  'mars_aspects_4th': 4 in mars_asp,
                'rahu_sign':          rahu_sign, 'rahu_house':    rahu_h,
                'ketu_sign':          ketu_sign, 'ketu_house':    ketu_h,
                'property_activation': '\n  '.join(activations) if activations else 'None',
            })

        return results
    except Exception as e:
        print(f'[Property transit history] Error: {e}')
        import traceback as _tb; _tb.print_exc()
        return []


# --------------------------------------------------
# Stress transit history — Saturn, Mars, Ketu, Sade Sati last N years
# --------------------------------------------------

def calculate_stress_transit_history(birth_houses, moon_sign, years_back=7):
    """Track stress indicators: Sade Sati, Saturn/Mars/Ketu in sensitive houses."""
    try:
        import swisseph as swe
        today = datetime.utcnow()

        sade_sati_signs = []
        if moon_sign in _SIGNS:
            mi = _SIGNS.index(moon_sign)
            sade_sati_signs = [_SIGNS[(mi - 1) % 12], moon_sign, _SIGNS[(mi + 1) % 12]]

        results = []
        for y in range(years_back, -1, -1):
            cd = today.replace(year=today.year - y, month=6, day=15)
            jd = swe.julday(cd.year, cd.month, cd.day, 12.0)
            swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
            ayanamsa = swe.get_ayanamsa_ut(jd)

            sat_sign,  sat_h  = _get_sign_and_house(jd, ayanamsa, swe.SATURN, birth_houses)
            mars_sign, mars_h = _get_sign_and_house(jd, ayanamsa, swe.MARS,   birth_houses)
            rahu_sign, rahu_h = _get_sign_and_house(jd, ayanamsa, swe.MEAN_NODE, birth_houses)
            ketu_h = ((rahu_h - 1 + 6) % 12) + 1 if rahu_h else None
            ketu_sign = _SIGNS[(_SIGNS.index(rahu_sign) + 6) % 12] if rahu_sign in _SIGNS else None

            sat_asp  = get_planet_aspects('Saturn', sat_h)
            mars_asp = get_planet_aspects('Mars',   mars_h)

            sade_sati = bool(sat_sign and sat_sign in sade_sati_signs)
            flags = []

            if sade_sati:
                phase = ('peak'   if sat_sign == moon_sign        else
                         'rising' if sat_sign == sade_sati_signs[0] else 'ending')
                flags.append(f'Sade Sati {phase} phase (Saturn in {sat_sign})')

            if sat_h in [1, 4, 8, 12]:
                labels = {1:'health and identity pressure', 4:'home and emotional stress',
                          8:'sudden events, transformation', 12:'isolation, losses, expenses'}
                flags.append(f'Saturn IN House {sat_h} — {labels[sat_h]}')
            else:
                for h in [1, 8, 12]:
                    if h in sat_asp:
                        flags.append(f'Saturn aspects H{h} ({describe_aspect("Saturn", sat_h, h)}) — stress on that area')

            if ketu_h in [1, 4, 8, 12]:
                labels = {1:'confusion and identity loss', 4:'emotional detachment',
                          8:'hidden fears and sudden change', 12:'spiritual crisis or isolation'}
                flags.append(f'Ketu IN House {ketu_h} — {labels[ketu_h]}')

            if mars_h in [1, 8]:
                labels = {1:'anger, accidents, health flare-up', 8:'sudden crisis or injury risk'}
                flags.append(f'Mars IN House {mars_h} — {labels[mars_h]}')
            else:
                for h in [1, 8]:
                    if h in mars_asp:
                        flags.append(f'Mars aspects H{h} ({describe_aspect("Mars", mars_h, h)}) — physical/health stress')

            results.append({
                'year':          cd.year,
                'saturn_sign':   sat_sign,  'saturn_house':  sat_h,  'saturn_aspects': sat_asp,
                'mars_sign':     mars_sign, 'mars_house':    mars_h, 'mars_aspects':   mars_asp,
                'ketu_sign':     ketu_sign, 'ketu_house':    ketu_h,
                'sade_sati':     sade_sati,
                'stress_flags':  flags,
                'stress_level':  ('HIGH' if len(flags) >= 3 else 'MODERATE' if flags else 'LOW'),
            })

        return results
    except Exception as e:
        print(f'[Stress transit history] Error: {e}')
        import traceback as _tb; _tb.print_exc()
        return []


# --------------------------------------------------
# Analyze-chart helpers (antardasha logic)
# --------------------------------------------------

VIMSHOTTARI_LORDS = ['Ketu', 'Venus', 'Sun', 'Moon', 'Mars', 'Rahu', 'Jupiter', 'Saturn', 'Mercury']
VIMSHOTTARI_YEARS = {'Ketu': 7, 'Venus': 20, 'Sun': 6, 'Moon': 10, 'Mars': 7,
                     'Rahu': 18, 'Jupiter': 16, 'Saturn': 19, 'Mercury': 17}


def calculate_antardashas_for_prompt(maha_dasha):
    try:
        planet = maha_dasha.get('planet')
        md_start = datetime.fromisoformat(maha_dasha.get('start_date'))
        md_end = datetime.fromisoformat(maha_dasha.get('end_date'))
        total_days = (md_end - md_start).days
        start_idx = VIMSHOTTARI_LORDS.index(planet)
        result = []
        cursor = md_start
        for i in range(9):
            ad_planet = VIMSHOTTARI_LORDS[(start_idx + i) % 9]
            ad_days = (total_days * VIMSHOTTARI_YEARS[ad_planet]) / 120
            ad_end = cursor + timedelta(days=ad_days)
            result.append({
                'planet': ad_planet,
                'start': cursor.date().isoformat(),
                'end': ad_end.date().isoformat(),
            })
            cursor = ad_end
        return result
    except Exception as e:
        print(f"calculate_antardashas_for_prompt error: {e}")
        return []


def find_active_antardasha(antardashas):
    now = datetime.utcnow()
    for ad in (antardashas or []):
        try:
            start = datetime.fromisoformat(ad.get('start') or ad.get('start_date', ''))
            end = datetime.fromisoformat(ad.get('end') or ad.get('end_date', ''))
            if start <= now <= end:
                return ad
        except Exception:
            continue
    return None


def get_current_antardasha_name(chart_data):
    try:
        maha = chart_data.get('current_dasha', {}).get('maha_dasha', {}).get('planet', 'Unknown')
        ads = chart_data.get('current_dasha', {}).get('antar_dashas', [])
        ad = find_active_antardasha(ads)
        if ad:
            return f"{maha}-{ad['planet']}"
        return maha
    except Exception:
        return 'Current Period'


def get_current_antardasha_date_range(chart_data):
    try:
        ads = chart_data.get('current_dasha', {}).get('antar_dashas', [])
        ad = find_active_antardasha(ads)
        if ad:
            def fmt(d):
                return datetime.fromisoformat(d).strftime('%B %Y')
            return f"{fmt(ad.get('start') or ad.get('start_date'))} – {fmt(ad.get('end') or ad.get('end_date'))}"
        md = chart_data.get('current_dasha', {}).get('maha_dasha', {})
        if md.get('start_date') and md.get('end_date'):
            return f"{md['start_date']} – {md['end_date']}"
        return 'Current Period'
    except Exception:
        return 'Current Period'


# --------------------------------------------------
# Flask routes
# --------------------------------------------------

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'pyjhora_available': PYJHORA_AVAILABLE,
        'pyjhora_import_error': PYJHORA_IMPORT_ERROR,
        'version': '3.0.0'
    })


@app.route('/api/generate-chart-pyjhora', methods=['POST'])
def generate_chart_route():
    if not PYJHORA_AVAILABLE:
        return jsonify({
            'error': 'PyJHora not available',
            'message': 'Please install: pip install PyJHora pyswisseph',
            'details': PYJHORA_IMPORT_ERROR
        }), 500
    try:
        data = request.json or {}
        # Accept either top-level fields or nested under chartData
        chart_input = data.get('chartData', data)
        name = chart_input.get('name', 'Unknown')
        birth_date = chart_input.get('date')
        birth_time = chart_input.get('time')
        latitude = float(chart_input.get('latitude'))
        longitude = float(chart_input.get('longitude'))
        timezone_offset = float(chart_input.get('timezone', 5.5))
        print(f"\nGenerating chart for: {name}")
        print(f"Date: {birth_date}, Time: {birth_time}")
        print(f"Location: {latitude}, {longitude}, TZ: {timezone_offset}")
        year, month, day = map(int, birth_date.split('-'))
        hour, minute = map(int, birth_time.split(':'))
        birth_dt = datetime(year, month, day, hour, minute)
        jd = compute_julian_day(year, month, day, hour, minute, timezone_offset)
        chart_data = calculate_chart(
            name=name, birth_dt=birth_dt,
            latitude=latitude, longitude=longitude,
            tz_offset_hours=timezone_offset, jd=jd
        )
        print("✓ Chart generated successfully!")

        # Inject verified Rahu/Ketu transit window from lookup table
        rahu_prev = rahu_curr = rahu_next = None
        today_dt  = datetime.utcnow()
        today_str = today_dt.strftime('%Y-%m-%d')
        for i, (start_str, end_str, r_sign, k_sign) in enumerate(RAHU_TRANSITS):
            start_dt = datetime.strptime(start_str, '%Y-%m-%d')
            end_dt   = datetime.strptime(end_str,   '%Y-%m-%d')
            if start_dt <= today_dt < end_dt:
                rahu_curr = {'rahu': r_sign, 'ketu': k_sign, 'from': start_str, 'to': end_str}
                if i > 0:
                    ps, pe, pr, pk = RAHU_TRANSITS[i - 1]
                    rahu_prev = {'rahu': pr, 'ketu': pk, 'from': ps, 'to': pe}
                if i < len(RAHU_TRANSITS) - 1:
                    ns, ne, nr, nk = RAHU_TRANSITS[i + 1]
                    rahu_next = {'rahu': nr, 'ketu': nk, 'from': ns, 'to': ne}
                break

        # Pull transit planet data (sign, nakshatra, house) from already-calculated transits
        transit_map = {}
        for p in (chart_data.get('data', {}).get('transits', {}).get('planets', []) or []):
            nak = p.get('nakshatra', '')
            transit_map[p['name']] = {
                'rasi':          p.get('rasi', 'Unknown'),
                'nakshatra':     nak,
                'nakshatra_lord': NAKSHATRA_LORDS.get(nak, 'Unknown') if nak else 'Unknown',
            }

        # Lagna nakshatra
        asc_data = chart_data.get('data', {}).get('ascendant', {})
        lagna_nak_obj = asc_data.get('nakshatra', {})
        lagna_nak_name = lagna_nak_obj.get('name', '') if isinstance(lagna_nak_obj, dict) else str(lagna_nak_obj)
        chart_data['data']['lagna_nakshatra']      = lagna_nak_name
        chart_data['data']['lagna_nakshatra_lord'] = NAKSHATRA_LORDS.get(lagna_nak_name, 'Unknown')

        def _tnk(planet_name):
            """Return nakshatra string for a transit planet."""
            d = transit_map.get(planet_name, {})
            nak = d.get('nakshatra', '')
            lord = d.get('nakshatra_lord', '')
            return (nak + ' (lord: ' + lord + ')') if nak else ''

        chart_data['data']['verified_current_transits'] = {
            'saturn':            transit_map.get('Saturn',  {}).get('rasi', 'Unknown'),
            'jupiter':           transit_map.get('Jupiter', {}).get('rasi', 'Unknown'),
            'rahu':              rahu_curr['rahu']  if rahu_curr else transit_map.get('Rahu', {}).get('rasi'),
            'ketu':              rahu_curr['ketu']  if rahu_curr else transit_map.get('Ketu', {}).get('rasi'),
            'rahu_from':         rahu_curr['from']  if rahu_curr else None,
            'rahu_to':           rahu_curr['to']    if rahu_curr else None,
            'rahu_prev':         rahu_prev,
            'rahu_next':         rahu_next,
            'date':              today_str,
            'saturn_nakshatra':  _tnk('Saturn'),
            'jupiter_nakshatra': _tnk('Jupiter'),
            'rahu_nakshatra':    _tnk('Rahu'),
        }

        # ── Dasha Sandhi detection ──
        try:
            _md_end_str = (chart_data.get('data', {}).get('dasha', {})
                           .get('current_dasha', {}).get('maha_dasha', {}).get('end_date'))
            if _md_end_str:
                _md_end_dt = datetime.fromisoformat(_md_end_str)
                _sandhi_days = (_md_end_dt - today_dt).days
                _sandhi_warning = 0 < _sandhi_days <= 240
                _sandhi_days_remaining = _sandhi_days if _sandhi_warning else None
            else:
                _sandhi_warning = False
                _sandhi_days_remaining = None
        except Exception:
            _sandhi_warning = False
            _sandhi_days_remaining = None
        chart_data['data']['dasha_sandhi_warning'] = _sandhi_warning
        chart_data['data']['dasha_sandhi_days_remaining'] = _sandhi_days_remaining

        return jsonify(chart_data)
    except Exception as e:
        print(f"✗ Error generating chart: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/analyze-chart', methods=['POST'])
def analyze_chart():
    try:
        chart_data = (request.json or {}).get('chartData', {})

        current_year = datetime.utcnow().year
        now = datetime.utcnow()
        seven_years_ago = now.replace(year=now.year - 7)

        ascendant_name = (chart_data.get('ascendant') or {}).get('rasi', {}).get('name') \
            or chart_data.get('ascendant') or 'Unknown'

        # Lagna nakshatra
        asc_nak_obj = (chart_data.get('ascendant') or {}).get('nakshatra', {})
        lagna_nak_name = asc_nak_obj.get('name', '') if isinstance(asc_nak_obj, dict) else str(asc_nak_obj)
        lagna_nak_lord = NAKSHATRA_LORDS.get(lagna_nak_name, 'Unknown')

        # Ensure antardashas are populated
        raw_ads = (chart_data.get('current_dasha') or {}).get('antar_dashas', [])
        if not raw_ads and (chart_data.get('current_dasha') or {}).get('maha_dasha'):
            chart_data['current_dasha']['antar_dashas'] = \
                calculate_antardashas_for_prompt(chart_data['current_dasha']['maha_dasha'])

        antardasha_name = get_current_antardasha_name(chart_data)
        antardasha_range = get_current_antardasha_date_range(chart_data)

        # Build past 7 years dasha list
        all_mahadashas = (chart_data.get('current_dasha') or {}).get('all_mahadashas', [])
        current_md = (chart_data.get('current_dasha') or {}).get('maha_dasha', {}).get('planet', '')
        current_ads = (chart_data.get('current_dasha') or {}).get('antar_dashas', [])
        past_7_dashas = []
        for md in all_mahadashas:
            try:
                md_start = datetime.fromisoformat(md['start'])
                md_end = datetime.fromisoformat(md['end'])
            except Exception:
                continue
            if md_end < seven_years_ago or md_start > now:
                continue
            if md['planet'] == current_md and current_ads:
                ads_to_use = current_ads
            else:
                ads_to_use = calculate_antardashas_for_prompt(
                    {'planet': md['planet'], 'start_date': md['start'], 'end_date': md['end']}
                )
            for ad in ads_to_use:
                try:
                    ad_start = datetime.fromisoformat(ad.get('start') or ad.get('start_date', ''))
                    ad_end = datetime.fromisoformat(ad.get('end') or ad.get('end_date', ''))
                except Exception:
                    continue
                if ad_end >= seven_years_ago and ad_start <= now:
                    past_7_dashas.append({
                        'period': f"{md['planet']}-{ad['planet']}",
                        'start': ad.get('start') or ad.get('start_date'),
                        'end': ad.get('end') or ad.get('end_date'),
                    })
        past_7_dashas.sort(key=lambda x: x['start'])

        # Build planet details
        planet_details = []
        for p in (chart_data.get('planets') or []):
            nak_obj = p.get('nakshatra') or {}
            nak_name = nak_obj.get('name', '') if isinstance(nak_obj, dict) else str(nak_obj)
            planet_details.append({
                'name': p.get('name'),
                'house': p.get('house'),
                'sign': (p.get('rasi') or {}).get('name') or p.get('sign'),
                'degree': round(float((p.get('position') or {}).get('degree', 0) or p.get('degree', 0)), 2),
                'retrograde': p.get('is_retrograde') or p.get('retrograde') or False,
                'nakshatra': nak_name,
                'nakshatra_lord': NAKSHATRA_LORDS.get(nak_name, '') if nak_name else ''
            })

        atmakaraka = max(planet_details, key=lambda p: p['degree']) if planet_details else \
            {'name': 'Unknown', 'degree': 0, 'sign': 'Unknown', 'house': 0}

        data_payload = {
            'birth_date': chart_data.get('date'),
            'birth_time': chart_data.get('time'),
            'current_age': current_year - int((chart_data.get('date') or '2000-01-01').split('-')[0]),
            'ascendant': {
                'sign': ascendant_name,
                'degree': round(float(chart_data.get('ascendant_degree') or
                                      (chart_data.get('ascendant') or {}).get('degree', 0) or 0), 2),
                'birth_star': lagna_nak_name,
                'birth_star_lord': lagna_nak_lord
            },
            'moon': {
                'sign': chart_data.get('moon_sign'),
                'nakshatra': chart_data.get('moon_nakshatra')
            },
            'atmakaraka': atmakaraka,
            'planets': planet_details,
            'houses': chart_data.get('houses', []),
            'd10_dasamsa': chart_data.get('d10_dasamsa'),
            'career_analysis': chart_data.get('career_analysis'),
            'current_mahadasha': (chart_data.get('current_dasha') or {}).get('maha_dasha'),
            'antardashas_sequence': (chart_data.get('current_dasha') or {}).get('antar_dashas', []),
            'current_antardasha': antardasha_name,
            'current_antardasha_period': antardasha_range,
        }

        def planet_house(name):
            p = next((x for x in planet_details if x['name'] == name), None)
            return p['house'] if p else '?'

        d10_planets = (chart_data.get('d10_dasamsa') or {}).get('planets', [])
        d10_text = '\n'.join(
            f"{p['name']:<10} | {p['sign']:<12} | Lord: {p['sign_lord']}" for p in d10_planets
        ) if d10_planets else 'D10 data not available'

        career_strengths = '\n'.join(
            (chart_data.get('career_analysis') or {}).get('professional_strengths', [])
        ) or 'Not available'

        past_dasha_text = '\n'.join(
            f"{d['period']}: {d['start']} to {d['end']}" for d in past_7_dashas
        ) if past_7_dashas else 'No antardasha data for last 7 years — use natal chart for past events'

        def _dasha_nak(lord_name):
            for p in planet_details:
                if (p.get('name') or '').lower() == lord_name.lower():
                    nak = p.get('nakshatra', '')
                    lord = p.get('nakshatra_lord', '?')
                    return f"{nak} (lord: {lord})" if nak else 'unknown'
            return 'unknown'

        planet_table = '\n'.join(
            f"{p['name']:<10} | House {p['house']} | {str(p['sign'] or ''):<12} | {p['degree']}° | {'R' if p['retrograde'] else 'D'} | Birth star: {p.get('nakshatra','?')} (ruled by {p.get('nakshatra_lord','?')})"
            for p in planet_details
        )

        md = (chart_data.get('current_dasha') or {}).get('maha_dasha') or {}

        # ── Active dasha period extraction for prompt ──
        _cd       = chart_data.get('current_dasha') or {}
        _ads      = _cd.get('antar_dashas', [])
        _active_ad = find_active_antardasha(_ads)
        _today    = now.date()

        def _days_remaining(date_str):
            try:
                return (date.fromisoformat(date_str) - _today).days
            except Exception:
                return '?'

        def _fmt_date(date_str):
            try:
                return datetime.fromisoformat(date_str).strftime('%Y-%m-%d')
            except Exception:
                return date_str or '?'

        _md_planet  = md.get('planet', 'Unknown')
        _md_start   = _fmt_date(md.get('start_date', ''))
        _md_end     = _fmt_date(md.get('end_date', ''))
        _md_days    = _days_remaining(md.get('end_date', ''))

        _ad_planet  = (_active_ad or {}).get('planet', antardasha_name or 'Unknown')
        _ad_start   = _fmt_date((_active_ad or {}).get('start') or (_active_ad or {}).get('start_date', ''))
        _ad_end     = _fmt_date((_active_ad or {}).get('end')   or (_active_ad or {}).get('end_date', ''))
        _ad_days    = _days_remaining((_active_ad or {}).get('end') or (_active_ad or {}).get('end_date', ''))

        _pt_planet  = _cd.get('pratyantar', '')
        _pt_start   = _fmt_date(_cd.get('pratyantar_start', ''))
        _pt_end     = _fmt_date(_cd.get('pratyantar_end', ''))
        _pt_days    = _days_remaining(_cd.get('pratyantar_end', ''))

        if _pt_planet:
            _pt_end_human = datetime.fromisoformat(_cd.get('pratyantar_end', _pt_end)).strftime('%B %Y') \
                if _cd.get('pratyantar_end') else _pt_end
        else:
            _pt_end_human = ''

        active_dasha_block = (
            f"════ ACTIVE DASHA PERIODS — USE THESE EXACT DATES ════\n"
            f"TODAY      : {_today}\n"
            f"MAHADASHA  : {_md_planet:<10} | {_md_start} \u2192 {_md_end}  (active — {_md_days} days remaining)\n"
            f"ANTARDASHA : {_ad_planet:<10} | {_ad_start} \u2192 {_ad_end}  (active — {_ad_days} days remaining)\n"
            + (f"PRATYANTAR : {_pt_planet:<10} | {_pt_start} \u2192 {_pt_end}  (active — {_pt_days} days remaining)\n"
               if _pt_planet else "PRATYANTAR : Not available\n")
            + f"════════════════════════════════════════════════════\n"
            f"RULE: Never calculate or invent dasha dates. Only use the exact dates shown above.\n"
            + (f"When mentioning the micro-period, always say '{_pt_planet} micro-period until {_pt_end_human}' — never any other date.\n"
               if _pt_planet else '')
        )

        # ── Sade Sati server-side calculation ──
        sign_order_ss = ['Aries','Taurus','Gemini','Cancer','Leo','Virgo',
                         'Libra','Scorpio','Sagittarius','Capricorn','Aquarius','Pisces']
        sade_sati_status = 'Not calculated'
        try:
            moon_sign_ss = chart_data.get('moon_sign', '')
            now_jd_ss = swe.julday(now.year, now.month, now.day, now.hour + now.minute / 60.0)
            swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
            sat_lon_ss, _ = compute_sidereal_planet_longitude(now_jd_ss, swe.SATURN)
            saturn_sign_ss = sign_order_ss[int(sat_lon_ss / 30.0)]
            if moon_sign_ss in sign_order_ss:
                moon_idx_ss = sign_order_ss.index(moon_sign_ss)
                sade_sati_signs = [
                    sign_order_ss[(moon_idx_ss + 11) % 12],
                    sign_order_ss[moon_idx_ss],
                    sign_order_ss[(moon_idx_ss + 1)  % 12]
                ]
                if saturn_sign_ss in sade_sati_signs:
                    phase_map_ss = {
                        sade_sati_signs[0]: 'Rising phase (12th from Moon) — preparing and letting go',
                        sade_sati_signs[1]: 'Peak phase (on Moon sign) — most intense pressure',
                        sade_sati_signs[2]: 'Ending phase (2nd from Moon) — rebuilding'
                    }
                    sade_sati_status = f"ACTIVE — {phase_map_ss[saturn_sign_ss]} (Saturn in {saturn_sign_ss}, Moon in {moon_sign_ss})"
                else:
                    sade_sati_status = f"Not active (Saturn in {saturn_sign_ss}, Moon in {moon_sign_ss})"
        except Exception as e:
            print(f'Sade Sati calc error: {e}')

        # Build property transit history text for the prompt
        prop_history_rows = chart_data.get('property_transit_history') or []
        if prop_history_rows:
            prop_lines = []
            for row in prop_history_rows:
                sat_h   = row.get('saturn_house',  '?')
                jup_h   = row.get('jupiter_house', '?')
                mars_h  = row.get('mars_house',    '?')
                sat_asp = ' (aspects 4th)' if row.get('saturn_aspects_4th')  else ''
                jup_asp = ' (aspects 4th)' if row.get('jupiter_aspects_4th') else ''
                mar_asp = ' (aspects 4th)' if row.get('mars_aspects_4th')    else ''
                activation = row.get('property_activation', 'None')
                line = (f"{row['year']}: Saturn H{sat_h}{sat_asp} | "
                        f"Jupiter H{jup_h}{jup_asp} | Mars H{mars_h}{mar_asp}")
                if activation and activation != 'None':
                    line += f"  ← {activation}"
                prop_lines.append(line)
            property_history_text = '\n'.join(prop_lines)
            print(f'[Analyze-chart] Property history ({len(prop_lines)} rows):\n' + property_history_text)
        else:
            property_history_text = 'Not available — recalculate from house positions above'
            print('[Analyze-chart] No property_transit_history in payload — will rely on raw houses')

        system_instruction = json.dumps({
            "role": "Friendly life guide who uses astrology as a tool",
            "framework": "Strategic Blueprint Framework v4.0",
            "mission": "Provide clear, plain-English insights using the provided chart data. Speak like a smart friend explaining things — no astrology textbook language.",
            "rules": [
                "Use EXACT house numbers from the JSON — never recalculate.",
                "Atmakaraka is the planet with the highest degree in the planets list.",
                "Only mention conjunctions if planets share the SAME house number.",
                "Do not say a planet is exalted/debilitated unless verified from the data.",
                f"Current year is {current_year}. Calculate the currently active life period, not the birth period.",
                "Write in plain English. No astrology jargon. Maximum 800 words total across all 3 sections.",
                "Each section maximum 2-3 bullet points. No long paragraphs.",
                "Speak directly to the person as if explaining to a smart friend with no astrology knowledge.",
                "Do not use technical terms like Dasha, Nakshatra, Lagna, Bindu, Rasi, Navamsa, Dasamsa, SAV, D9, D10 — use plain English equivalents instead.",
                "Say 'life period' not Dasha, 'rising sign' not Lagna, 'sub-period' not Antardasha, 'strength score' not Bindu.",
                f"BIRTH STAR RULE: Always name the birth star by its actual name (e.g., Hasta, Chitra, Anuradha). The person's rising sign birth star is '{lagna_nak_name}' (ruled by {lagna_nak_lord}). In the PERSONALITY section you MUST write '{lagna_nak_name}' by name — never just say 'birth star' without naming it. One sentence max.",
                "CRITICAL: Do NOT add any greeting, preamble, introduction, or closing message. Start the response immediately with # 🎭 PERSONALITY and nothing before it.",
                "CRITICAL: Produce exactly 3 sections only. No 4th section. No future predictions. Maximum 250 words total.",
                "For the MAJOR EVENTS section always check ALL 4 categories. Never skip a category even if nothing happened — write 'No major activation in this area.' if nothing significant occurred.",
                "Always check 8th house and Ketu activation for stress periods. Always check Sade Sati overlap with stress periods. Always check 4th house activation for property. Always check 7th house for relationship events.",
                "RULE 6 — DASHA DATES: Never calculate dasha or micro-period dates yourself. Always read them from the ACTIVE DASHA PERIODS block at the top of the user message. The micro-period planet is always the one listed under PRATYANTAR in that block, and its end date is always the date shown there — never any other date."
            ]
        })

        user_prompt = f"""
MANDATORY: Use ONLY the data below. Do not recalculate house positions.

PLANET POSITIONS:
{planet_table}

VERIFICATION:
- Sun   → House {planet_house('Sun')}
- Moon  → House {planet_house('Moon')}
- Mars  → House {planet_house('Mars')}
- Mercury → House {planet_house('Mercury')}
- Jupiter → House {planet_house('Jupiter')}
- Saturn  → House {planet_house('Saturn')}

BIRTH DATA:
{json.dumps(data_payload, indent=2)}

D10 DASAMSA CHART (Career Blueprint):
{d10_text}

Career Analysis from D10:
{career_strengths}

DASHA PERIODS — LAST 7 YEARS ({seven_years_ago.year}–{now.year}):
{past_dasha_text}

{active_dasha_block}
  Mahadasha lord birth star: {_dasha_nak(_md_planet)}
  Sub-period lord birth star: {_dasha_nak(_ad_planet)}

RISING SIGN BIRTH STAR: {lagna_nak_name} (ruled by {lagna_nak_lord})
- Use this to add ONE specific personality texture beyond just the rising sign
- E.g. if Anuradha (Saturn-ruled): disciplined loyalty, slow-burning intensity, devotion with structure
- Weave the birth star quality into the personality description — do NOT list it as a data point

Birth Data:
- Date: {chart_data.get('date')}
- Time: {chart_data.get('time')}
- Place: {chart_data.get('place', 'Unknown')}
- Ascendant: {ascendant_name}
- Ashtakavarga: {json.dumps(chart_data.get('ashtakavarga'))}

Produce EXACTLY 3 sections using these exact headers. No preamble. No greeting. Start immediately with # 🎭 PERSONALITY. No other sections.

# 🎭 PERSONALITY
Write exactly 7-8 sentences in flowing prose — absolutely no bullets, no dashes, no numbered points.

Structure the response across these 3 layers:

LAYER 1 — THE OPERATING SYSTEM (2 sentences):
Describe how this person comes across to the world — the rising sign, lagna nakshatra {lagna_nak_name}, and every planet sitting in the 1st house. If multiple planets occupy the 1st house, name them and describe how they combine into a single outward personality. Give this layer a short evocative name in parentheses — e.g. "The Precision Engine" or "The Structured Visionary".
CRITICAL: Layer 1 must be exactly 2 separate sentences ending with a full stop each. Never combine them into one long compound sentence with em-dashes or commas. End the second sentence with the archetype name in parentheses.

LAYER 2 — THE EMOTIONAL INTERIOR (2-3 sentences):
Describe how this person feels on the inside — the Moon sign, Moon nakshatra, and Moon house. If Moon is the Atmakaraka (highest degree planet), say so explicitly and explain what it means: their soul's deepest mission is expressed through their emotional nature. Describe the tension or contrast between the outer personality (Layer 1) and the inner emotional world — this contrast is often the most relatable part. Give this layer an evocative name.

LAYER 3 — THE SHADOW SIDE (2 sentences):
Name one or two blind spots or patterns that hold this person back. Draw from debilitated planets, challenging house placements, or the shadow expression of strong planets. Be direct but compassionate — frame it as "the shadow of your greatest strength" not as a flaw. Give this layer an evocative name.

CAREER FIELD — your final sentence MUST start with the literal text "Career field:" and use this exact format with a pipe character separating two fields:
Career field: [Primary field name] — strongly indicated | [Secondary field name] — secondary fit
FORBIDDEN: Do not write "Your primary career field is..." or any natural-language sentence. Do not use percentages or brackets. The line must start with "Career field:" — this is a hard requirement.

TONE: Write as if you are a wise astrologer speaking directly to this person — warm, specific, and surprising. Every sentence should feel like something only their chart could reveal, not generic astrology advice.
Maximum 200 words for this section.

# 🔍 MAJOR EVENTS - LAST 7 YEARS ({current_year - 7}–{current_year})
Analyze the last 7 years using the dasha periods, transits, and house activations. Report on ALL 4 categories below:

1. STRESS & HEALTH PERIOD
Was there a period of stress, anxiety, depression, health issues, job loss, or financial loss? If yes: when did it peak, what planetary combination caused it, and is it still active or has it passed?

2. GAINS & GROWTH PERIOD
Was there a period of career growth, promotion, income increase, business success, or financial gains? If yes: when did it occur and what planetary combination supported it?

3. PROPERTY & ASSETS
PROPERTY TRANSIT DATA (last 7 years — pre-calculated, use this directly):
{property_history_text}

Check ALL 4 property triggers using the data above:
TRIGGER 1: Any year where Saturn H4 or "(aspects 4th)" — Saturn in or aspecting 4th house
TRIGGER 2: Any year where Jupiter H4 or "(aspects 4th)" — Jupiter in or aspecting 4th house
TRIGGER 3: Any year where Mars H4 or "(aspects 4th)" — Mars in or aspecting 4th house
TRIGGER 4: Is the current or recent Mahadasha/Antardasha lord the 4th house lord, or does it sit in the 4th house?
If ANY trigger is confirmed — name the year and what likely happened. Never say no activation if any trigger is true.

4. RELATIONSHIPS
Was there a period of harmony or significant stress in relationships, marriage, or partnerships? Any major relationship event like marriage, separation, or a significant new partnership? If yes: when and what caused it?

Format: Put each category number and name on its OWN line, then write the content starting on the NEXT line (never on the same line as the category header). Write 2 sentences maximum per category. NEVER write "No major activation in this area" or any variation — if a house shows no planetary triggers, write 1-2 sentences on what the quiet energy means: energy consolidating, a period of inward focus, or what to expect when activation next arrives. Make every period feel meaningful.
Use plain English. No jargon. Maximum 120 words for this section.

# ⚡ CURRENT CYCLE & FOCUS
3-4 bullet points on the current period themes and what to prioritise right now.
Name the active sub-period and what it specifically means for this person.
Maximum 80 words for this section.

IMPORTANT: No Next 12 Months section. No Next 5 Years section. No future predictions section.
Total response maximum 400 words.
Person is {data_payload['current_age']} years old in {current_year}.
Atmakaraka is {atmakaraka['name']} (highest degree: {atmakaraka['degree']}°).

STRESS & MENTAL PRESSURE CONTEXT:
Sade Sati status: {sade_sati_status}
8th house lord: (derive from house data above)

RULE: If discussing health, mental wellbeing, or emotional themes, always check and mention:
- Whether Sade Sati is active and what phase it is in
- Whether the 8th house lord is active in the current life period
- Whether Ketu is active in any current period
Frame all stress triggers as transformation opportunities, not misfortune.

VEDIC ASPECT RULES (Graha Drishti — apply to all transit and natal analysis):
- ALL planets aspect the 7th house from their position (opposition aspect)
- SATURN special aspects: 3rd and 10th house from its position (in addition to 7th)
- JUPITER special aspects: 5th and 9th house from its position (in addition to 7th)
- MARS special aspects: 4th and 8th house from its position (in addition to 7th)
- Rahu/Ketu aspect: 5th and 9th from their positions (like Jupiter)
- NEVER say a planet does not aspect a house without checking these special aspects first
- Example: Saturn in H1 aspects H3, H7, H10 — so Saturn in H1 pressures 10th house career
"""

        if USE_XAI and xai_client:
            # ── xAI Grok ──
            response = xai_client.chat.completions.create(
                model='grok-3-mini',
                messages=[
                    {'role': 'system', 'content': system_instruction},
                    {'role': 'user',   'content': user_prompt}
                ],
                max_tokens=1800,
                temperature=0.65
            )
            analysis_text = response.choices[0].message.content.strip()
            analysis_text = analysis_text.replace('```json', '').replace('```', '').strip()
            print(f'[Grok] analyze-chart tokens: {response.usage.total_tokens}')
            return Response(analysis_text, mimetype='text/plain')

        else:
            # ── Gemini fallback ──
            api_key = os.environ.get('GEMINI_API_KEY')
            if not api_key:
                return jsonify({'error': 'GEMINI_API_KEY not set'}), 500
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                model_name='gemini-2.5-flash',
                system_instruction=system_instruction,
                generation_config={'temperature': 0.65, 'max_output_tokens': 8192}
            )
            response = model.generate_content(user_prompt)
            analysis_text = response.text.strip()
            analysis_text = analysis_text.replace('```json', '').replace('```', '').strip()
            return Response(analysis_text, mimetype='text/plain')

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    try:
        data           = request.json or {}
        system_prompt  = data.get('system', '')
        messages       = data.get('messages', [])
        session_id     = data.get('session_id', '')
        is_bubble      = data.get('is_bubble', False)
        model_override = data.get('model_override', None)

        # Track question in session store
        if session_id and session_id in session_store:
            for msg in reversed(messages):
                if msg.get('role') == 'user':
                    question = msg.get('content', '').strip()
                    if question:
                        q_num  = len(session_store[session_id]['questions']) + 1
                        prefix = '[Bubble]' if is_bubble else '[Chat]'
                        session_store[session_id]['questions'].append(f'{q_num}. {prefix} {question}')
                    break

        if USE_XAI and xai_client:
            # ── xAI Grok ──
            grok_messages = [{'role': 'system', 'content': system_prompt}]
            for msg in messages:
                role = 'assistant' if msg.get('role') == 'assistant' else 'user'
                grok_messages.append({'role': role, 'content': msg.get('content', '')})

            model_to_use = model_override or 'grok-3-mini'
            response = xai_client.chat.completions.create(
                model=model_to_use,
                messages=grok_messages,
                max_tokens=500,
                temperature=0.65
            )
            answer = response.choices[0].message.content
            token_data = {
                'input':  response.usage.prompt_tokens,
                'output': response.usage.completion_tokens,
                'total':  response.usage.total_tokens,
                'model':  model_to_use
            }
            print(f'[Grok] chat ({model_to_use}) tokens: {response.usage.total_tokens}')
            return jsonify({'answer': answer, 'tokens': token_data})

        else:
            # ── Gemini fallback ──
            api_key = os.environ.get('GEMINI_API_KEY')
            if not api_key:
                return jsonify({'error': 'GEMINI_API_KEY not set'}), 500
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                model_name='gemini-2.5-flash',
                system_instruction=system_prompt if system_prompt else None,
                generation_config={'temperature': 0.65, 'max_output_tokens': 8192}
            )
            history = []
            for msg in messages:
                role = 'model' if msg.get('role') == 'assistant' else msg.get('role', 'user')
                history.append({'role': role, 'parts': [msg.get('content', '')]})
            last_user_message = ''
            if history and history[-1]['role'] in ('user', 'model'):
                last_user_message = history[-1]['parts'][0]
                history = history[:-1]
            chat_session = model.start_chat(history=history)
            response = chat_session.send_message(last_user_message)
            token_data = {
                'input':  response.usage_metadata.prompt_token_count if response.usage_metadata else 0,
                'output': response.usage_metadata.candidates_token_count if response.usage_metadata else 0,
                'total':  response.usage_metadata.total_token_count if response.usage_metadata else 0,
                'model':  'gemini-2.5-flash'
            }
            return jsonify({'answer': response.text, 'tokens': token_data})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/ping-start', methods=['POST'])
def session_start():
    try:
        session_id = str(uuid.uuid4())[:8]
        session_store[session_id] = {
            'session_id':        session_id,
            'timestamp':         datetime.utcnow().isoformat(),
            'birth_date':        '',
            'birth_time':        '',
            'birth_place':       '',
            'chart_generated':   False,
            'analysis_provided': False,
            'bubble_questions':  0,
            'chat_questions':    0,
            'error_log':         '',
            'questions':         [],
        }
        print(f'[Session] Started: {session_id}')
        return jsonify({'session_id': session_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ping-event', methods=['POST'])
def session_event():
    try:
        data       = request.json or {}
        session_id = data.get('session_id', '')
        event      = data.get('event', '')

        if session_id not in session_store:
            print(f'[Session] Event received for unknown session {session_id}')
            return jsonify({'status': 'unknown_session'})

        sess = session_store[session_id]

        if event == 'chart_generated':
            sess['chart_generated'] = True
            sess['birth_date']      = data.get('birth_date', '')
            sess['birth_time']      = data.get('birth_time', '')
            sess['birth_place']     = data.get('birth_place', '')
            print(f'[Session] {session_id} — chart generated')

        elif event == 'analysis_provided':
            sess['analysis_provided'] = True
            print(f'[Session] {session_id} — analysis provided')

        elif event == 'question_asked':
            question  = data.get('question', '').strip()
            is_bubble = data.get('is_bubble', False)
            if is_bubble:
                sess['bubble_questions'] += 1
            else:
                sess['chat_questions'] += 1
            if question:
                q_num  = len(sess['questions']) + 1
                prefix = '[Bubble]' if is_bubble else '[Chat]'
                sess['questions'].append(f'{q_num}. {prefix} {question}')
                print(f'[Session] {session_id} — question: {question[:50]}')

        elif event == 'error':
            sess['error_log'] = data.get('error_message', '')[:200]

        return jsonify({'status': 'ok'})
    except Exception as e:
        print(f'[Session] Event error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/ping-end', methods=['POST'])
def session_end():
    try:
        try:
            data = request.json or {}
        except Exception:
            data = json.loads(request.data.decode('utf-8')) if request.data else {}
        session_id   = data.get('session_id', '')
        fe_questions = data.get('questions', [])

        if session_id and session_id in session_store:
            sess = session_store[session_id]
            if not sess['questions'] and fe_questions:
                sess['questions'] = [f'{i+1}. {q}' for i, q in enumerate(fe_questions)]
            log_to_notion(sess)
            del session_store[session_id]
            print(f'[Session] Logged and cleared {session_id}')

        elif fe_questions or session_id:
            fallback = {
                'session_id':        session_id or 'recovered',
                'timestamp':         datetime.utcnow().isoformat(),
                'birth_date':        data.get('birth_date', ''),
                'birth_time':        data.get('birth_time', ''),
                'birth_place':       data.get('birth_place', ''),
                'chart_generated':   data.get('chart_generated', False),
                'analysis_provided': data.get('analysis_provided', False),
                'bubble_questions':  sum(1 for q in fe_questions if '[Bubble]' in q),
                'chat_questions':    sum(1 for q in fe_questions if '[Chat]' in q),
                'error_log':         '',
                'questions':         [f'{i+1}. {q}' for i, q in enumerate(fe_questions)],
            }
            log_to_notion(fallback)
            print(f'[Session] Cold-start fallback logged for {session_id}')

        return jsonify({'status': 'logged'})
    except Exception as e:
        print(f'[Session] session-end error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/api/notion-schema', methods=['GET'])
def notion_schema():
    """Return the exact property names and types from the Notion database."""
    token       = os.environ.get("NOTION_TOKEN", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            db = json.loads(resp.read().decode("utf-8"))
            props = {k: v["type"] for k, v in db.get("properties", {}).items()}
            return jsonify({"properties": props})
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.code, "body": json.loads(e.read().decode())}), 200


@app.route('/api/test-notion', methods=['GET'])
def test_notion():
    """Test Notion connectivity — returns the actual API response or error."""
    token       = os.environ.get("NOTION_TOKEN", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")

    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Timestamp":         {"title": [{"text": {"content": datetime.utcnow().isoformat()}}]},
            "Session ID":        {"rich_text": [{"text": {"content": "TEST-001"}}]},
            "Birth Date":        {"rich_text": [{"text": {"content": "1990-01-01"}}]},
            "Birth Place":       {"rich_text": [{"text": {"content": "TestCity"}}]},
            "Chart Generated":   {"checkbox": True},
            "Analysis provided": {"checkbox": True},
            "Questions Asked":   {"rich_text": [{"text": {"content": "1. [bubble] Test question"}}]},
            "Error log":         {"rich_text": [{"text": {"content": ""}}]},
        }
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            resp_body = resp.read().decode("utf-8")
            return jsonify({"status": "success", "notion_response": json.loads(resp_body)})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return jsonify({"status": "error", "http_code": e.code, "notion_error": json.loads(error_body)}), 200
    except Exception as e:
        return jsonify({"status": "exception", "error": str(e)}), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'\n✅ Server running on: http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)

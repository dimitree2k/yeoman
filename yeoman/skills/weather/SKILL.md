---
name: weather
description: Get current weather and forecasts (no API key required). Primary: Open-Meteo (EU-hosted). Fallback: wttr.in.
metadata: {"yeoman":{"emoji":"🌤️","requires":{"bins":["curl"]}}}
---

# Weather

Two free services, no API keys needed.

## Open-Meteo (primary)

EU-hosted (Switzerland), uses ECMWF/DWD models. Excellent for Germany/Europe.

### Known coordinates (use directly, no geocoding needed)

| City | latitude | longitude |
|------|----------|-----------|
| Düsseldorf | 51.2217 | 6.7762 |
| Frankfurt | 50.1109 | 8.6821 |
| Berlin | 52.5200 | 13.4050 |
| Hamburg | 53.5753 | 10.0153 |
| München | 48.1351 | 11.5820 |

### Current weather
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.2217&longitude=6.7762&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m&wind_speed_unit=kmh&timezone=Europe%2FBerlin"
```

### Hourly forecast (next 24h)
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.2217&longitude=6.7762&hourly=temperature_2m,precipitation_probability,weather_code&wind_speed_unit=kmh&timezone=Europe%2FBerlin&forecast_days=1"
```

### Daily forecast (7 days)
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.2217&longitude=6.7762&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max&wind_speed_unit=kmh&timezone=Europe%2FBerlin"
```

### WMO weather codes (key ones)
`0` Clear · `1-3` Partly cloudy · `45,48` Fog · `51-55` Drizzle · `61-65` Rain · `71-75` Snow · `80-82` Showers · `95` Thunderstorm

Docs: https://open-meteo.com/en/docs

### For other cities — geocode with Nominatim (OSM)
```bash
curl -s "https://nominatim.openstreetmap.org/search?q=Cologne&format=json&limit=1" | python3 -c "import sys,json; r=json.load(sys.stdin)[0]; print(r['lat'], r['lon'])"
```

## wttr.in (fallback)

Use when you need a quick one-liner or Open-Meteo is unavailable.

```bash
curl -s "wttr.in/Düsseldorf?format=%l:+%c+%t+%h+%w&m"
# Output: Düsseldorf: ⛅️ +12°C 65% ↙18km/h
```

Full forecast:
```bash
curl -s "wttr.in/Berlin?T&m"
```

Format codes: `%c` condition · `%t` temp · `%h` humidity · `%w` wind · `%m` moon · `?m` metric · `?1` today only

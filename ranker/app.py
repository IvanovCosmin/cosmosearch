"""
Search Result Ranker Proxy

A proxy service that sits in front of SearXNG and re-ranks results based on:
- Date/year (pre-2022 content gets boosted)
- Site factuality (known low-quality sites get penalized)
"""

import re
import requests
from datetime import datetime
from urllib.parse import urlparse, urlencode
from flask import Flask, request, jsonify, render_template_string
from langdetect import detect, LangDetectException

app = Flask(__name__)

# Configuration
SEARXNG_URL = "http://localhost:8080"

# Year-based scoring configuration
PIVOT_YEAR = 2022
POST_2022_PENALTY = 3  # Flat penalty for results after 2022

# Low factuality sites and their penalty scores (0-100, higher = more penalty)
LOW_FACTUALITY_SITES = {
    # Misinformation / Conspiracy sites
    'infowars.com': 5,
    'naturalnews.com': 5,
    'beforeitsnews.com': 5,
    'globalresearch.ca': 75,
    'zerohedge.com': 5,
    'thegatewaypundit.com': 5,
    'breitbart.com': 5,
    'dailywire.com': 5,
    'oann.com': 5,
    'newsmax.com': 5    ,
    
    # Clickbait / Low quality
    'buzzfeed.com': 5,
    'dailymail.co.uk': 5,
    'thesun.co.uk': 5,
    'nypost.com': 5,
    'express.co.uk': 5,
    
    'rt.com': 5,
    'sputniknews.com': 5,
}

# High factuality sites get a boost
HIGH_FACTUALITY_SITES = {
    'reuters.com': 2,
    'apnews.com': 2,
    'bbc.com': 2,
    'bbc.co.uk': 2,
    'npr.org': 2,
    'pbs.org': 2,
    'nature.com': 2,
    'science.org': 2,
    'sciencedirect.com': 2,
    'pubmed.ncbi.nlm.nih.gov': 2,
    'arxiv.org': 2,
    'jstor.org': 2,
    'wikipedia.org': 2,
    'britannica.com': 2,
    'snopes.com': 2,
    'factcheck.org': 2,
    'politifact.com': 2,
    'aljazeera.com': 2,
    'theguardian.com': 2,
}

# TLD bonuses
TLD_BONUSES = {
    'gov': 1,
    'edu': 1,
    'gov.uk': 1
}

PREFERRED_LANGUAGES = {
    'en': 1,  
    'ro': 1,  
}
NON_PREFERRED_LANGUAGE_PENALTY = 1 


def extract_year_from_result(result):
    """Extract publication year from result metadata."""
    # Try publishedDate first
    published = result.get('publishedDate', '')
    if published:
        # Try various date formats
        for fmt in ['%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%B %d, %Y', '%d %B %Y']:
            try:
                date_str = published[:19] if 'T' in published else published[:10]
                return datetime.strptime(date_str, fmt[:len(date_str)]).year
            except (ValueError, IndexError):
                continue
        
        # Extract year with regex
        year_match = re.search(r'\b(19|20)\d{2}\b', published)
        if year_match:
            return int(year_match.group())
    
    # Try to extract from content/title
    content = f"{result.get('content', '')} {result.get('title', '')}"
    year_match = re.search(r'\b(20[0-2][0-9]|201[0-9]|200[0-9]|199[0-9])\b', content)
    if year_match:
        year = int(year_match.group())
        current_year = datetime.now().year
        if 1990 <= year <= current_year:
            return year
    
    return None


def get_domain(url):
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except:
        return None


def get_tld(domain):
    """Extract TLD from domain."""
    if domain:
        parts = domain.split('.')
        if len(parts) >= 1:
            return parts[-1]
    return None


def calculate_year_score(year):
    """Calculate score adjustment based on year.
    
    Pre-2022: No adjustment (neutral)
    Post-2022: Flat penalty
    """
    if year is None:
        return 0
    
    if year <= PIVOT_YEAR:
        return 0  # No boost or penalty for pre-2022 content
    else:
        return -POST_2022_PENALTY  # Flat penalty for post-2022 content


def calculate_factuality_score(domain):
    """Calculate score based on site factuality."""
    if not domain:
        return 0
    
    # Check low factuality sites
    for site, penalty in LOW_FACTUALITY_SITES.items():
        if domain == site or domain.endswith('.' + site):
            return -penalty
    
    # Check high factuality sites
    for site, boost in HIGH_FACTUALITY_SITES.items():
        if domain == site or domain.endswith('.' + site):
            return boost
    
    # Check TLD bonuses
    tld = get_tld(domain)
    if tld in TLD_BONUSES:
        return TLD_BONUSES[tld]
    
    return 0


def detect_language(result):
    """Detect language from result content and metadata."""
    # First, check if SearXNG provided language metadata
    lang = result.get('language', '').lower()[:2] if result.get('language') else None
    if lang:
        return lang
    
    # Try to detect from content + title
    text = f"{result.get('title', '')} {result.get('content', '')}"
    if len(text.strip()) < 20:  # Not enough text to detect
        return None
    
    try:
        detected = detect(text)
        return detected
    except LangDetectException:
        return None


def calculate_language_score(lang):
    """Calculate score adjustment based on language.
    
    English and Romanian get a boost, other languages get penalized.
    """
    if lang is None:
        return 0  # Unknown language, no adjustment
    
    lang = lang.lower()[:2]  # Normalize to 2-letter code
    
    if lang in PREFERRED_LANGUAGES:
        return PREFERRED_LANGUAGES[lang]
    else:
        return -NON_PREFERRED_LANGUAGE_PENALTY


def rank_results(results):
    """Re-rank results based on custom scoring."""
    scored_results = []
    total_results = len(results)
    
    for idx, result in enumerate(results):
        url = result.get('url', '')
        domain = get_domain(url)
        year = extract_year_from_result(result)
        lang = detect_language(result)
        
        # Position score: first result gets max points, decreasing by 1 for each position
        # E.g., with 100 results: 1st = 100pts, 2nd = 99pts, ..., 100th = 1pt
        position_score = max(1, total_results - idx)
        
        # Calculate other scores
        year_score = calculate_year_score(year)
        factuality_score = calculate_factuality_score(domain)
        language_score = calculate_language_score(lang)
        total_score = position_score + year_score + factuality_score + language_score
        
        # Store scoring details
        result['_score'] = total_score
        result['_score_details'] = {
            'position': idx + 1,
            'position_score': position_score,
            'year': year,
            'year_score': year_score,
            'domain': domain,
            'factuality_score': factuality_score,
            'language': lang,
            'language_score': language_score,
        }
        
        scored_results.append(result)
    
    # Sort by score (descending), maintaining original order for ties
    scored_results.sort(key=lambda x: x.get('_score', 0), reverse=True)
    
    return scored_results


# HTML template for the search interface
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SearXNG with Smart Ranking</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e8e8e8;
        }
        
        .container {
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
        }
        
        header {
            text-align: center;
            padding: 40px 0 30px;
        }
        
        h1 {
            font-size: 2.5em;
            background: linear-gradient(90deg, #00d9ff, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 10px;
        }
        
        .subtitle {
            color: #888;
            font-size: 0.95em;
        }
        
        .search-box {
            display: flex;
            gap: 10px;
            margin: 30px 0;
        }
        
        input[type="text"] {
            flex: 1;
            padding: 15px 20px;
            border: 2px solid #333;
            border-radius: 12px;
            background: rgba(255,255,255,0.05);
            color: #fff;
            font-size: 1.1em;
            transition: all 0.3s;
        }
        
        input[type="text"]:focus {
            outline: none;
            border-color: #00d9ff;
            box-shadow: 0 0 20px rgba(0,217,255,0.2);
        }
        
        button {
            padding: 15px 30px;
            background: linear-gradient(135deg, #00d9ff, #00ff88);
            border: none;
            border-radius: 12px;
            color: #1a1a2e;
            font-weight: bold;
            font-size: 1em;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(0,217,255,0.3);
        }
        
        .result {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            padding: 20px;
            margin: 15px 0;
            transition: all 0.3s;
        }
        
        .result:hover {
            background: rgba(255,255,255,0.06);
            border-color: rgba(0,217,255,0.3);
        }
        
        .result-title {
            font-size: 1.2em;
            margin-bottom: 8px;
        }
        
        .result-title a {
            color: #00d9ff;
            text-decoration: none;
        }
        
        .result-title a:hover {
            text-decoration: underline;
        }
        
        .result-url {
            color: #00ff88;
            font-size: 0.85em;
            margin-bottom: 8px;
            word-break: break-all;
        }
        
        .result-content {
            color: #aaa;
            line-height: 1.6;
        }
        
        .score-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.8em;
            margin-top: 12px;
        }
        
        .score-positive { background: rgba(0,255,136,0.15); color: #00ff88; }
        .score-negative { background: rgba(255,100,100,0.15); color: #ff6464; }
        .score-neutral { background: rgba(255,255,255,0.1); color: #888; }
        
        .score-detail {
            display: inline-block;
            padding: 3px 8px;
            margin: 2px;
            border-radius: 4px;
            font-size: 0.75em;
            background: rgba(255,255,255,0.05);
        }
        
        .score-detail.score-penalty {
            background: rgba(255,80,80,0.2);
            color: #ff6464;
        }
        
        .score-detail.score-boost {
            background: rgba(0,255,136,0.15);
            color: #00ff88;
        }
        
        .engine-badge {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75em;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background: rgba(138, 43, 226, 0.2);
            color: #b388ff;
            margin-right: 8px;
        }
        
        .result-meta {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 10px;
        }
        
        .no-results {
            text-align: center;
            padding: 60px 20px;
            color: #666;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
        }
        
        .legend {
            background: rgba(255,255,255,0.03);
            border-radius: 12px;
            padding: 20px;
            margin: 20px 0;
            font-size: 0.9em;
        }
        
        .legend h3 {
            color: #00d9ff;
            margin-bottom: 10px;
        }
        
        .legend-item {
            margin: 8px 0;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🔍 Smart Search</h1>
            <p class="subtitle">SearXNG with date-based ranking & factuality scoring</p>
        </header>
        
        <form action="/search" method="GET" class="search-box">
            <input type="text" name="q" placeholder="Search the web..." value="{{ query or '' }}" autofocus>
            <button type="submit">Search</button>
        </form>
        
        {% if show_legend %}
        <div class="legend">
            <h3>📊 Ranking System</h3>
            <div class="legend-item"># <strong>Position Score:</strong> Original ranking matters - 1st result gets 100 pts, 2nd gets 99, etc.</div>
            <div class="legend-item">🌐 <strong>Language Priority:</strong> English & Romanian results get +50 pts, other languages get -80 pts</div>
            <div class="legend-item">📅 <strong>Year Penalty:</strong> Results from after 2022 get -30 pts penalty</div>
            <div class="legend-item">✅ <strong>Factuality Boost:</strong> Trusted sources (reuters, bbc, .gov, .edu) get +10-20 pts</div>
            <div class="legend-item">⚠️ <strong>Factuality Penalty:</strong> Low-quality/misinformation sites get -20 to -90 pts</div>
        </div>
        {% endif %}
        
        {% if results %}
            {% for result in results %}
            <div class="result">
                <div class="result-meta">
                    {% if result.engine %}
                    <span class="engine-badge">{{ result.engine }}</span>
                    {% endif %}
                    <span class="result-url">{{ result.pretty_url or result.url }}</span>
                </div>
                <div class="result-title">
                    <a href="{{ result.url }}" target="_blank">{{ result.title }}</a>
                </div>
                <div class="result-content">{{ result.content }}</div>
                
                {% set score = result._score or 0 %}
                {% set details = result._score_details or {} %}
                <div class="score-badge {{ 'score-positive' if score > 0 else ('score-negative' if score < 0 else 'score-neutral') }}">
                    Score: {{ score }}
                    {% if details.position_score %}
                    <span class="score-detail score-boost">#{{ details.position }} (+{{ details.position_score }})</span>
                    {% endif %}
                    {% if details.language %}
                    <span class="score-detail {{ 'score-penalty' if details.language_score < 0 else 'score-boost' }}">🌐 {{ details.language.upper() }} ({{ '%+d' % details.language_score }})</span>
                    {% endif %}
                    {% if details.year %}
                    <span class="score-detail {{ 'score-penalty' if details.year_score < 0 else 'score-boost' }}">📅 {{ details.year }} ({{ '%+d' % details.year_score }})</span>
                    {% endif %}
                    {% if details.factuality_score != 0 %}
                    <span class="score-detail {{ 'score-penalty' if details.factuality_score < 0 else 'score-boost' }}">{{ '✅' if details.factuality_score > 0 else '⚠️' }} {{ '%+d' % details.factuality_score }}</span>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        {% elif query %}
            <div class="no-results">
                <p>No results found for "{{ query }}"</p>
            </div>
        {% endif %}
    </div>
</body>
</html>
'''


@app.route('/')
def home():
    """Render the search homepage."""
    return render_template_string(HTML_TEMPLATE, query=None, results=None, show_legend=True)


@app.route('/search')
def search():
    """Proxy search to SearXNG and re-rank results."""
    query = request.args.get('q', '')
    
    if not query:
        return render_template_string(HTML_TEMPLATE, query=None, results=None, show_legend=True)
    
    try:
        # Query SearXNG's JSON API
        params = {
            'q': query,
            'format': 'json',
        }
        
        response = requests.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        # Re-rank results
        ranked_results = rank_results(results)
        
        return render_template_string(HTML_TEMPLATE, query=query, results=ranked_results, show_legend=False)
    
    except requests.RequestException as e:
        return render_template_string(HTML_TEMPLATE, 
                                      query=query, 
                                      results=None, 
                                      show_legend=False,
                                      error=str(e))


@app.route('/api/search')
def api_search():
    """JSON API endpoint for search."""
    query = request.args.get('q', '')
    
    if not query:
        return jsonify({'error': 'No query provided'}), 400
    
    try:
        params = {
            'q': query,
            'format': 'json',
        }
        
        response = requests.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        ranked_results = rank_results(results)
        
        return jsonify({
            'query': query,
            'results': ranked_results,
            'count': len(ranked_results),
        })
    
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config')
def api_config():
    """Return current ranking configuration."""
    return jsonify({
        'pivot_year': PIVOT_YEAR,
        'post_2022_penalty': POST_2022_PENALTY,
        'low_factuality_sites': LOW_FACTUALITY_SITES,
        'high_factuality_sites': HIGH_FACTUALITY_SITES,
        'tld_bonuses': TLD_BONUSES,
        'preferred_languages': PREFERRED_LANGUAGES,
        'non_preferred_language_penalty': NON_PREFERRED_LANGUAGE_PENALTY,
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)


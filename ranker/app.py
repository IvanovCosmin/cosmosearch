"""
Search Result Ranker Proxy

A proxy service that sits in front of SearXNG and re-ranks results based on:
- Date/year (pre-2022 content gets boosted)
- Site factuality (known low-quality sites get penalized)
"""

import os
import re
import requests
from datetime import datetime
from urllib.parse import urlparse
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# Configuration
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
RESULTS_PER_PAGE = 35

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
    'quora.com': 5,
    'medium.com': 5,
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

# Torrent engine penalties
TORRENT_ENGINES = {
    'bt4g': -5,
} 


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
    """Detect language from result content and metadata.
    
    Uses a fast heuristic approach instead of slow langdetect library.
    Only falls back to langdetect for ambiguous cases if needed.
    """
    # First, check if SearXNG provided language metadata
    lang = result.get('language', '').lower()[:2] if result.get('language') else None
    if lang:
        return lang
    
    # Get text to analyze
    text = f"{result.get('title', '')} {result.get('content', '')}"
    if len(text.strip()) < 20:
        return None
    
    # Fast heuristic: check for Romanian-specific characters/patterns
    # Romanian has: ă, â, î, ș, ț and common words
    romanian_chars = set('ăâîșțĂÂÎȘȚ')
    romanian_words = {'și', 'sau', 'pentru', 'este', 'sunt', 'care', 'acest', 'într', 'unei', 'unui', 'precum', 'despre'}
    
    text_lower = text.lower()
    
    # Check for Romanian characters
    if any(c in text for c in romanian_chars):
        return 'ro'
    
    # Check for common Romanian words
    words = set(text_lower.split())
    if len(words & romanian_words) >= 2:
        return 'ro'
    
    # Fast English detection: check for common English patterns
    english_words = {'the', 'and', 'for', 'that', 'with', 'this', 'from', 'have', 'are', 'was', 'been', 'which', 'their', 'will', 'would', 'could', 'should'}
    if len(words & english_words) >= 2:
        return 'en'
    
    # For other languages, just return None (neutral score)
    # This avoids the slow langdetect call for most results
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


def calculate_engine_score(engine):
    """Calculate score adjustment based on search engine.
    
    Torrent engines get penalized.
    """
    if not engine:
        return 0
    
    engine_lower = engine.lower()
    return TORRENT_ENGINES.get(engine_lower, 0)


def rank_results(results):
    """Re-rank results based on custom scoring."""
    scored_results = []
    total_results = len(results)
    
    for idx, result in enumerate(results):
        url = result.get('url', '')
        domain = get_domain(url)
        year = extract_year_from_result(result)
        lang = detect_language(result)
        engine = result.get('engine', '')
        
        # Position score: first result gets max points, decreasing by 1 for each position
        # E.g., with 100 results: 1st = 100pts, 2nd = 99pts, ..., 100th = 1pt
        position_score = max(1, total_results - idx)
        
        # Calculate other scores
        year_score = calculate_year_score(year)
        factuality_score = calculate_factuality_score(domain)
        language_score = calculate_language_score(lang)
        engine_score = calculate_engine_score(engine)
        total_score = position_score + year_score + factuality_score + language_score + engine_score
        
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
            'engine': engine,
            'engine_score': engine_score,
        }
        
        scored_results.append(result)
    
    # Sort by score (descending), maintaining original order for ties
    scored_results.sort(key=lambda x: x.get('_score', 0), reverse=True)
    
    return scored_results


def get_dictionary_definition(word):
    """
    Fetch dictionary definition from Free Dictionary API.
    Returns a snippet dict or None.
    """
    try:
        # Free Dictionary API - no API key required
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
        response = requests.get(url, timeout=3)
        if response.ok:
            data = response.json()
            if data and isinstance(data, list) and len(data) > 0:
                entry = data[0]
                word_title = entry.get('word', word)
                phonetic = entry.get('phonetic', '')
                
                # Get meanings
                meanings = entry.get('meanings', [])
                definitions = []
                for meaning in meanings[:2]:  # Limit to 2 parts of speech
                    part_of_speech = meaning.get('partOfSpeech', '')
                    defs = meaning.get('definitions', [])
                    if defs:
                        def_text = defs[0].get('definition', '')
                        example = defs[0].get('example', '')
                        if def_text:
                            entry_text = f"({part_of_speech}) {def_text}"
                            if example:
                                entry_text += f' — "{example}"'
                            definitions.append(entry_text)
                
                if definitions:
                    content = ' │ '.join(definitions)
                    if phonetic:
                        content = f"{phonetic}  {content}"
                    
                    return {
                        'type': 'definition',
                        'title': word_title.title(),
                        'content': content,
                        'source': 'Dictionary',
                        'source_url': f"https://www.dictionary.com/browse/{word}",
                        'image': None,
                    }
    except requests.RequestException:
        pass
    return None


def is_dictionary_query(query):
    """Check if the query looks like a dictionary/definition request."""
    q = query.lower().strip()
    
    # Explicit definition patterns
    patterns = ['define ', 'definition of ', 'meaning of ', 'what does ', 'what is the meaning of ']
    for pattern in patterns:
        if q.startswith(pattern):
            return True, q[len(pattern):].strip().split()[0] if q[len(pattern):].strip() else None
    
    # Single word queries (likely dictionary lookups)
    words = q.split()
    if len(words) == 1 and len(q) >= 3 and q.isalpha():
        return True, q
    
    return False, None


def get_knowledge_snippet(query):
    """
    Fetch a knowledge snippet (Featured Snippet) for the query.
    
    Tries multiple sources:
    1. Dictionary API for definition queries
    2. DuckDuckGo Instant Answer API 
    3. Wikipedia REST API for encyclopedic content
    """
    snippet = None
    
    # Check if this is a dictionary query first
    is_dict_query, word = is_dictionary_query(query)
    if is_dict_query and word:
        snippet = get_dictionary_definition(word)
        if snippet:
            return snippet
    
    # Try DuckDuckGo Instant Answer API
    try:
        ddg_url = "https://api.duckduckgo.com/"
        params = {
            'q': query,
            'format': 'json',
            'no_html': 1,
            'skip_disambig': 1,
        }
        response = requests.get(ddg_url, params=params, timeout=3)
        if response.ok:
            data = response.json()
            
            # Check for Abstract (usually from Wikipedia)
            if data.get('Abstract'):
                snippet = {
                    'type': 'encyclopedia',
                    'title': data.get('Heading', query),
                    'content': data.get('Abstract'),
                    'source': data.get('AbstractSource', 'Wikipedia'),
                    'source_url': data.get('AbstractURL'),
                    'image': data.get('Image'),
                }
            
            # Check for Definition (usually from Wiktionary)
            elif data.get('Definition'):
                snippet = {
                    'type': 'definition',
                    'title': data.get('Heading', query),
                    'content': data.get('Definition'),
                    'source': data.get('DefinitionSource', 'Wiktionary'),
                    'source_url': data.get('DefinitionURL'),
                    'image': None,
                }
            
            # Check for Answer (calculations, conversions, etc.)
            elif data.get('Answer'):
                snippet = {
                    'type': 'answer',
                    'title': query,
                    'content': data.get('Answer'),
                    'source': 'Instant Answer',
                    'source_url': None,
                    'image': None,
                }
            
            # Check for Infobox data
            elif data.get('Infobox') and data['Infobox'].get('content'):
                infobox = data['Infobox']['content']
                # Format infobox as key-value pairs
                info_text = ' • '.join([f"{item.get('label', '')}: {item.get('value', '')}" 
                                        for item in infobox[:5] if item.get('value')])
                if info_text:
                    snippet = {
                        'type': 'infobox',
                        'title': data.get('Heading', query),
                        'content': info_text,
                        'source': 'DuckDuckGo',
                        'source_url': data.get('AbstractURL'),
                        'image': data.get('Image'),
                    }
    except requests.RequestException:
        pass
    
    # If DDG returned Wikipedia, try to get a richer summary from Wikipedia API
    if snippet and snippet.get('source') == 'Wikipedia':
        try:
            # Extract the title from the Wikipedia URL or use the heading
            wiki_title = snippet.get('title', query).replace(' ', '_')
            wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_title}"
            response = requests.get(wiki_url, timeout=3)
            if response.ok:
                wiki_data = response.json()
                # Use the richer Wikipedia extract if it's longer
                wiki_extract = wiki_data.get('extract', '')
                if len(wiki_extract) > len(snippet.get('content', '')):
                    snippet['content'] = wiki_extract
                # Get a better thumbnail if available
                if wiki_data.get('thumbnail'):
                    snippet['image'] = wiki_data['thumbnail'].get('source')
                # Update URL to the actual page
                if wiki_data.get('content_urls', {}).get('desktop', {}).get('page'):
                    snippet['source_url'] = wiki_data['content_urls']['desktop']['page']
        except requests.RequestException:
            pass
    
    # If no DDG result, try Wikipedia directly for common query patterns
    if not snippet:
        # Check if query looks like a "what is" or definitional query
        q_lower = query.lower().strip()
        patterns = ['what is ', 'who is ', 'who was ']
        clean_query = q_lower
        for pattern in patterns:
            if q_lower.startswith(pattern):
                clean_query = q_lower[len(pattern):].strip()
                break
        
        try:
            wiki_title = clean_query.replace(' ', '_')
            wiki_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_title}"
            response = requests.get(wiki_url, timeout=3)
            if response.ok:
                wiki_data = response.json()
                if wiki_data.get('extract') and wiki_data.get('type') != 'disambiguation':
                    snippet = {
                        'type': 'encyclopedia',
                        'title': wiki_data.get('title', query),
                        'content': wiki_data.get('extract'),
                        'source': 'Wikipedia',
                        'source_url': wiki_data.get('content_urls', {}).get('desktop', {}).get('page'),
                        'image': wiki_data.get('thumbnail', {}).get('source') if wiki_data.get('thumbnail') else None,
                    }
        except requests.RequestException:
            pass
    
    return snippet


# Available search engines/categories
SEARCH_TABS = [
    {'id': 'general', 'name': '[all]', 'engines': '', 'categories': 'general'},
    {'id': 'images', 'name': '[img]', 'engines': 'google images,bing images,duckduckgo images', 'categories': 'images'},
    {'id': 'videos', 'name': '[vid]', 'engines': '', 'categories': 'videos'},
    {'id': 'news', 'name': '[news]', 'engines': '', 'categories': 'news'},
    {'id': 'science', 'name': '[sci]', 'engines': '', 'categories': 'science'},
    {'id': 'files', 'name': '[files]', 'engines': 'annas archive,z-library,bt4g', 'categories': 'files'},
]

# HTML template for the search interface
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% if query %}{{ query }} - {% endif %}CosmoSearch</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect fill='%230a0a0a' width='32' height='32'/%3E%3Cpath fill='%2300ff41' d='M24 6H10a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h14v-4H12V10h12V6z'/%3E%3Cpath fill='%2300ff41' opacity='0.6' d='M12 10h4v4h-4zM12 18h4v4h-4z'/%3E%3C/svg%3E">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0a0a0a;
            --bg-alt: #0d1117;
            --bg-card: #111318;
            --green: #00ff41;
            --green-dim: #00cc33;
            --cyan: #00d4ff;
            --magenta: #ff00ff;
            --yellow: #ffff00;
            --red: #ff3333;
            --orange: #ff9500;
            --white: #c9d1d9;
            --gray: #6e7681;
            --border: #30363d;
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: 'JetBrains Mono', 'Fira Code', monospace;
            background: var(--bg);
            min-height: 100vh;
            color: var(--white);
            font-size: 14px;
            line-height: 1.6;
        }
        
        /* Scanline effect */
        body::before {
            content: "";
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            pointer-events: none;
            background: repeating-linear-gradient(
                0deg,
                rgba(0, 0, 0, 0.15),
                rgba(0, 0, 0, 0.15) 1px,
                transparent 1px,
                transparent 2px
            );
            z-index: 1000;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px 40px;
        }
        
        /* Header */
        .header {
            border-bottom: 1px solid var(--border);
            padding: 15px 0;
            margin-bottom: 20px;
        }
        
        .header-row {
            display: flex;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        
        /* ASCII Logo */
        .logo {
            text-decoration: none;
            white-space: pre;
            font-size: 10px;
            line-height: 1.2;
            color: var(--green);
            text-shadow: 0 0 10px var(--green);
        }
        
        .logo-small {
            color: var(--cyan);
            text-decoration: none;
            font-size: 16px;
            font-weight: bold;
            text-shadow: 0 0 8px var(--cyan);
        }
        
        .logo-small::before {
            content: "$ ";
            color: var(--green);
        }
        
        /* Search box */
        .search-form {
            flex: 1;
            min-width: 300px;
        }
        
        .search-box {
            display: flex;
            align-items: center;
            background: var(--bg-alt);
            border: 1px solid var(--border);
            padding: 0;
        }
        
        .search-prompt {
            color: var(--green);
            padding: 10px 0 10px 12px;
            user-select: none;
        }
        
        .search-input {
            flex: 1;
            border: none;
            background: transparent;
            color: var(--white);
            font-family: inherit;
            font-size: 14px;
            padding: 10px 8px;
            outline: none;
        }
        
        .search-input::placeholder {
            color: var(--gray);
        }
        
        .search-btn {
            background: var(--green);
            border: none;
            color: var(--bg);
            font-family: inherit;
            font-weight: bold;
            padding: 10px 16px;
            cursor: pointer;
            transition: all 0.2s;
        }
        
        .search-btn:hover {
            background: var(--cyan);
            box-shadow: 0 0 15px var(--cyan);
        }
        
        /* Tabs */
        .tabs {
            display: flex;
            gap: 4px;
            margin-top: 15px;
            flex-wrap: wrap;
        }
        
        .tab {
            padding: 6px 12px;
            color: var(--gray);
            text-decoration: none;
            font-size: 13px;
            border: 1px solid transparent;
            transition: all 0.2s;
        }
        
        .tab:hover {
            color: var(--cyan);
            border-color: var(--border);
        }
        
        .tab.active {
            color: var(--green);
            border-color: var(--green);
            text-shadow: 0 0 8px var(--green);
        }
        
        /* Results info */
        .results-info {
            color: var(--gray);
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 1px dashed var(--border);
        }
        
        .results-info span {
            color: var(--cyan);
        }
        
        /* Results */
        .result {
            display: flex;
            gap: 16px;
            margin-bottom: 24px;
            padding: 16px;
            border-left: 2px solid var(--border);
            transition: all 0.2s;
        }
        
        .result:hover {
            border-left-color: var(--green);
            background: rgba(0, 255, 65, 0.02);
        }
        
        .result-thumb {
            flex-shrink: 0;
            width: 120px;
            height: 90px;
            border: 1px solid var(--border);
            background: var(--bg-alt);
            overflow: hidden;
        }
        
        .result-thumb:empty {
            display: none;
        }
        
        .result-thumb img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            filter: saturate(0.7) brightness(0.9);
            transition: all 0.3s;
        }
        
        .result:hover .result-thumb img {
            filter: saturate(1) brightness(1);
        }
        
        .result-body {
            flex: 1;
            min-width: 0;
        }
        
        .result-url-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 4px;
        }
        
        .favicon {
            width: 16px;
            height: 16px;
            flex-shrink: 0;
            border-radius: 2px;
        }
        
        .result-url {
            color: var(--gray);
            font-size: 12px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .result-url::before {
            content: "→ ";
            color: var(--green);
        }
        
        .result-title {
            font-size: 16px;
            margin-bottom: 8px;
        }
        
        .result-title a {
            color: var(--cyan);
            text-decoration: none;
            text-shadow: 0 0 1px var(--cyan);
        }
        
        .result-title a:hover {
            color: var(--green);
            text-shadow: 0 0 8px var(--green);
        }
        
        .result-title a:visited {
            color: var(--magenta);
        }
        
        .result-content {
            color: var(--white);
            font-size: 14px;
            line-height: 1.7;
            margin-bottom: 10px;
        }
        
        /* Meta tags */
        .result-meta {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            font-size: 11px;
        }
        
        .tag {
            padding: 2px 8px;
            border: 1px solid var(--border);
            color: var(--gray);
        }
        
        /* Engine brand colors */
        .tag.engine { border-color: var(--magenta); color: var(--magenta); }
        .tag.engine-google { border-color: #4285f4; color: #4285f4; }
        .tag.engine-bing { border-color: #00809d; color: #00809d; }
        .tag.engine-duckduckgo { border-color: #de5833; color: #de5833; }
        .tag.engine-yahoo { border-color: #6001d2; color: #6001d2; }
        .tag.engine-brave { border-color: #fb542b; color: #fb542b; }
        .tag.engine-wikipedia { border-color: #636466; color: #a2a9b1; }
        .tag.engine-reddit { border-color: #ff4500; color: #ff4500; }
        .tag.engine-youtube { border-color: #ff0000; color: #ff0000; }
        .tag.engine-github { border-color: #6e5494; color: #a479e2; }
        .tag.engine-arxiv { border-color: #b31b1b; color: #b31b1b; }
        .tag.engine-stackoverflow { border-color: #f48024; color: #f48024; }
        .tag.engine-wolframalpha { border-color: #dd1100; color: #dd1100; }
        .tag.engine-qwant { border-color: #5c97ff; color: #5c97ff; }
        .tag.engine-startpage { border-color: #6573ff; color: #6573ff; }
        .tag.engine-mojeek { border-color: #007c91; color: #007c91; }
        .tag.engine-yandex { border-color: #ffcc00; color: #ffcc00; }
        .tag.engine-baidu { border-color: #2932e1; color: #2932e1; }
        .tag.engine-ecosia { border-color: #3faa4d; color: #3faa4d; }
        .tag.engine-swisscows { border-color: #e10050; color: #e10050; }
        
        .tag.score {
            border-color: var(--cyan);
            color: var(--cyan);
            position: relative;
            cursor: help;
        }
        
        .tag.boost {
            border-color: var(--green);
            color: var(--green);
        }
        
        .tag.penalty {
            border-color: var(--red);
            color: var(--red);
        }
        
        .tag.year {
            border-color: var(--yellow);
            color: var(--yellow);
        }
        
        .tag.lang {
            border-color: var(--orange);
            color: var(--orange);
        }
        
        /* Score tooltip */
        .score-tooltip {
            position: relative;
            cursor: help;
        }
        
        .score-tooltip .tooltip-content {
            display: none;
            position: absolute;
            bottom: 100%;
            left: 0;
            margin-bottom: 8px;
            background: var(--bg);
            border: 1px solid var(--green);
            padding: 12px;
            min-width: 220px;
            z-index: 100;
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.2);
        }
        
        .score-tooltip:hover .tooltip-content {
            display: block;
        }
        
        .tooltip-header {
            color: var(--green);
            border-bottom: 1px dashed var(--border);
            padding-bottom: 8px;
            margin-bottom: 8px;
            font-weight: bold;
        }
        
        .tooltip-row {
            display: flex;
            justify-content: space-between;
            margin: 4px 0;
        }
        
        .tooltip-label {
            color: var(--gray);
        }
        
        .tooltip-value {
            color: var(--white);
        }
        
        .tooltip-value.positive { color: var(--green); }
        .tooltip-value.negative { color: var(--red); }
        .tooltip-value.neutral { color: var(--gray); }
        
        .tooltip-total {
            border-top: 1px solid var(--border);
            margin-top: 8px;
            padding-top: 8px;
            color: var(--cyan);
            font-weight: bold;
        }
        
        /* Image grid - full width */
        .image-grid-fullwidth {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 8px;
            padding: 20px;
            width: 100%;
        }
        
        .image-result {
            position: relative;
            border: 1px solid var(--border);
            background: var(--bg-alt);
            overflow: hidden;
            aspect-ratio: 1;
        }
        
        .image-result:hover {
            border-color: var(--green);
            box-shadow: 0 0 10px rgba(0, 255, 65, 0.3);
        }
        
        .image-result img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            filter: saturate(0.8);
            transition: all 0.3s;
        }
        
        .image-result:hover img {
            filter: saturate(1);
        }
        
        .image-result a {
            display: block;
            width: 100%;
            height: 100%;
        }
        
        .image-overlay {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 8px;
            background: linear-gradient(transparent, rgba(0,0,0,0.9));
            opacity: 0;
            transition: opacity 0.2s;
        }
        
        .image-result:hover .image-overlay {
            opacity: 1;
        }
        
        .image-title {
            font-size: 11px;
            color: var(--green);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .image-source {
            font-size: 10px;
            color: var(--gray);
        }
        
        /* Pagination */
        .pagination {
            display: flex;
            gap: 12px;
            margin: 30px 0;
            padding-top: 20px;
            border-top: 1px dashed var(--border);
        }
        
        .page-btn {
            padding: 8px 16px;
            background: transparent;
            border: 1px solid var(--border);
            color: var(--cyan);
            text-decoration: none;
            font-family: inherit;
            transition: all 0.2s;
        }
        
        .page-btn:hover {
            border-color: var(--green);
            color: var(--green);
            text-shadow: 0 0 8px var(--green);
        }
        
        .page-info {
            padding: 8px 16px;
            color: var(--gray);
        }
        
        /* No results */
        .no-results {
            padding: 40px 0;
            color: var(--red);
        }
        
        .no-results::before {
            content: "[ERROR] ";
            color: var(--red);
        }
        
        /* Homepage */
        .home-container {
            max-width: 900px;
            margin: 0 auto;
            padding: 60px 30px;
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
            justify-content: center;
        }
        
        .ascii-logo {
            color: var(--green);
            font-size: 8px;
            line-height: 1.1;
            white-space: pre;
            text-shadow: 0 0 20px var(--green);
            margin-bottom: 30px;
        }
        
        @media (max-width: 600px) {
            .ascii-logo { font-size: 5px; }
        }
        
        .home-search {
            width: 100%;
            max-width: 600px;
            margin-bottom: 30px;
        }
        
        .home-info {
            width: 100%;
            max-width: 600px;
            border: 1px solid var(--border);
            padding: 20px;
            background: var(--bg-alt);
        }
        
        .info-header {
            color: var(--cyan);
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
            margin-bottom: 15px;
        }
        
        .info-row {
            display: flex;
            margin: 8px 0;
        }
        
        .info-label {
            color: var(--magenta);
            width: 120px;
            flex-shrink: 0;
        }
        
        .info-value {
            color: var(--white);
        }
        
        .info-value.green { color: var(--green); }
        .info-value.yellow { color: var(--yellow); }
        .info-value.red { color: var(--red); }
        
        .blink {
            animation: blink 1s step-end infinite;
        }
        
        @keyframes blink {
            50% { opacity: 0; }
        }
        
        /* Autocomplete */
        .search-wrapper {
            position: relative;
            flex: 1;
            min-width: 300px;
        }
        
        .autocomplete-dropdown {
            position: absolute;
            top: 100%;
            left: 0;
            right: 0;
            background: var(--bg-alt);
            border: 1px solid var(--green);
            border-top: none;
            z-index: 1000;
            max-height: 300px;
            overflow-y: auto;
            display: none;
        }
        
        .autocomplete-dropdown.active {
            display: block;
        }
        
        .autocomplete-item {
            padding: 10px 12px;
            cursor: pointer;
            border-bottom: 1px solid var(--border);
            color: var(--white);
            transition: all 0.1s;
        }
        
        .autocomplete-item:last-child {
            border-bottom: none;
        }
        
        .autocomplete-item:hover,
        .autocomplete-item.selected {
            background: rgba(0, 255, 65, 0.1);
            color: var(--green);
        }
        
        .autocomplete-item::before {
            content: "› ";
            color: var(--cyan);
        }
        
        .autocomplete-item.selected::before {
            content: "» ";
            color: var(--green);
        }
        
        /* Knowledge Snippet / Featured Answer */
        .knowledge-card {
            background: var(--bg-alt);
            border: 1px solid var(--cyan);
            margin-bottom: 24px;
            position: relative;
            overflow: hidden;
        }
        
        .knowledge-card::before {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, var(--cyan), var(--green), var(--cyan));
            animation: knowledge-glow 2s ease-in-out infinite;
        }
        
        @keyframes knowledge-glow {
            0%, 100% { opacity: 0.5; }
            50% { opacity: 1; }
        }
        
        .knowledge-header {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 16px;
            border-bottom: 1px dashed var(--border);
            background: rgba(0, 212, 255, 0.05);
        }
        
        .knowledge-icon {
            color: var(--cyan);
            font-size: 12px;
        }
        
        .knowledge-label {
            color: var(--cyan);
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .knowledge-type {
            color: var(--gray);
            font-size: 11px;
            margin-left: auto;
        }
        
        .knowledge-body {
            display: flex;
            gap: 20px;
            padding: 16px;
        }
        
        .knowledge-image {
            flex-shrink: 0;
            width: 120px;
            height: 120px;
            border: 1px solid var(--border);
            background: var(--bg);
            overflow: hidden;
        }
        
        .knowledge-image img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            filter: saturate(0.8);
        }
        
        .knowledge-content {
            flex: 1;
            min-width: 0;
        }
        
        .knowledge-title {
            color: var(--green);
            font-size: 18px;
            font-weight: bold;
            margin-bottom: 10px;
            text-shadow: 0 0 10px rgba(0, 255, 65, 0.3);
        }
        
        .knowledge-text {
            color: var(--white);
            font-size: 14px;
            line-height: 1.7;
            margin-bottom: 12px;
        }
        
        .knowledge-text.definition::before {
            content: "› ";
            color: var(--magenta);
        }
        
        .knowledge-source {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            color: var(--gray);
            font-size: 12px;
            text-decoration: none;
            padding: 4px 10px;
            border: 1px solid var(--border);
            transition: all 0.2s;
        }
        
        .knowledge-source:hover {
            border-color: var(--cyan);
            color: var(--cyan);
        }
        
        .knowledge-source::before {
            content: "→";
            color: var(--green);
        }
        
        .knowledge-source-icon {
            width: 14px;
            height: 14px;
            border-radius: 2px;
        }
    </style>
</head>
<body>
    {% if not query %}
    <!-- Homepage -->
    <div class="home-container">
        <pre class="ascii-logo">
   ██████╗ ██████╗ ███████╗███╗   ███╗ ██████╗ ███████╗███████╗ █████╗ ██████╗  ██████╗██╗  ██╗
  ██╔════╝██╔═══██╗██╔════╝████╗ ████║██╔═══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║
  ██║     ██║   ██║███████╗██╔████╔██║██║   ██║███████╗█████╗  ███████║██████╔╝██║     ███████║
  ██║     ██║   ██║╚════██║██║╚██╔╝██║██║   ██║╚════██║██╔══╝  ██╔══██║██╔══██╗██║     ██╔══██║
  ╚██████╗╚██████╔╝███████║██║ ╚═╝ ██║╚██████╔╝███████║███████╗██║  ██║██║  ██║╚██████╗██║  ██║
   ╚═════╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝ ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝
        </pre>
        
        <form action="/search" method="GET" class="home-search">
            <div class="search-wrapper">
                <div class="search-box">
                    <span class="search-prompt">search@cosmo:~$</span>
                    <input type="text" name="q" class="search-input" id="search-home" placeholder="enter query..." autofocus autocomplete="off">
                    <button type="submit" class="search-btn">EXEC</button>
                </div>
                <div class="autocomplete-dropdown" id="autocomplete-home"></div>
            </div>
        </form>
        
        {% if show_legend %}
        <div class="home-info">
            <div class="info-header">┌── system.info ──────────────────────────────</div>
            <div class="info-row">
                <span class="info-label">ranking</span>
                <span class="info-value green">smart re-ranking enabled</span>
            </div>
            <div class="info-row">
                <span class="info-label">position</span>
                <span class="info-value">original order preserved as base</span>
            </div>
            <div class="info-row">
                <span class="info-label">language</span>
                <span class="info-value green">EN/RO boosted</span>
            </div>
            <div class="info-row">
                <span class="info-label">date_filter</span>
                <span class="info-value yellow">post-2022 penalized</span>
            </div>
            <div class="info-row">
                <span class="info-label">factuality</span>
                <span class="info-value green">trusted sources boosted</span>
            </div>
            <div class="info-row">
                <span class="info-label">status</span>
                <span class="info-value green">● online<span class="blink">_</span></span>
            </div>
        </div>
        {% endif %}
    </div>
    
    {% else %}
    <!-- Search Results Page -->
    <div class="container">
        <div class="header">
            <div class="header-row">
                <a href="/" class="logo-small">CosmoSearch</a>
                
                <form action="/search" method="GET" class="search-form">
                    <div class="search-wrapper">
                        <div class="search-box">
                            <span class="search-prompt">~$</span>
                            <input type="text" name="q" class="search-input" id="search-results" value="{{ query }}" autocomplete="off">
                            <input type="hidden" name="tab" value="{{ current_tab }}">
                            <button type="submit" class="search-btn">EXEC</button>
                        </div>
                        <div class="autocomplete-dropdown" id="autocomplete-results"></div>
                    </div>
                </form>
            </div>
            
            <div class="tabs">
                {% for tab in tabs %}
                <a href="/search?q={{ query }}&tab={{ tab.id }}" class="tab {{ 'active' if current_tab == tab.id else '' }}">{{ tab.name }}</a>
                {% endfor %}
            </div>
        </div>
        
        {% if results %}
            <div class="results-info">found <span>{{ results|length }}</span> results for "<span>{{ query }}</span>"</div>
            
            {% if knowledge and current_tab == 'general' %}
            <!-- Knowledge Snippet / Featured Answer -->
            <div class="knowledge-card">
                <div class="knowledge-header">
                    <span class="knowledge-icon">◆</span>
                    <span class="knowledge-label">Knowledge Base</span>
                    <span class="knowledge-type">{{ knowledge.type | upper }}</span>
                </div>
                <div class="knowledge-body">
                    {% if knowledge.image %}
                    <div class="knowledge-image">
                        <img src="{{ knowledge.image }}" alt="{{ knowledge.title }}" onerror="this.parentElement.remove()">
                    </div>
                    {% endif %}
                    <div class="knowledge-content">
                        <div class="knowledge-title">{{ knowledge.title }}</div>
                        <div class="knowledge-text {{ 'definition' if knowledge.type == 'definition' else '' }}">{{ knowledge.content[:500] }}{% if knowledge.content|length > 500 %}...{% endif %}</div>
                        {% if knowledge.source_url %}
                        <a href="{{ knowledge.source_url }}" target="_blank" class="knowledge-source">
                            <img class="knowledge-source-icon" src="https://www.google.com/s2/favicons?domain={{ knowledge.source_url }}&sz=32" alt="" onerror="this.style.display='none'">
                            {{ knowledge.source }}
                        </a>
                        {% else %}
                        <span class="knowledge-source">{{ knowledge.source }}</span>
                        {% endif %}
                    </div>
                </div>
            </div>
            {% endif %}
            
            {% if current_tab == 'images' %}
            <!-- Image Grid - Full Width -->
            </div>
            <div class="image-grid-fullwidth">
                {% for result in results %}
                {% if result.img_src or result.thumbnail %}
                <div class="image-result" data-img="{{ result.img_src or result.thumbnail }}">
                    <a href="{{ result.url }}" target="_blank">
                        <img src="{{ result.img_src or result.thumbnail }}" alt="{{ result.title }}" loading="lazy" onerror="this.closest('.image-result').remove()">
                        <div class="image-overlay">
                            <div class="image-title">{{ result.title[:40] }}{% if result.title|length > 40 %}...{% endif %}</div>
                            <div class="image-source">{{ result._score_details.domain or 'unknown' }}</div>
                        </div>
                    </a>
                </div>
                {% endif %}
                {% endfor %}
            </div>
            <div class="container">
            {% else %}
            <!-- Regular Results -->
            {% for result in results %}
            <div class="result">
                {% if result.img_src or result.thumbnail %}
                <div class="result-thumb">
                    <img src="{{ result.img_src or result.thumbnail }}" alt="" loading="lazy" onerror="this.parentElement.remove()">
                </div>
                {% endif %}
                
                <div class="result-body">
                    <div class="result-url-row">
                        <img class="favicon" src="https://www.google.com/s2/favicons?domain={{ result._score_details.domain or '' }}&sz=32" alt="" onerror="this.style.display='none'">
                        <span class="result-url">{{ result._score_details.domain or 'unknown' }} :: {{ result.pretty_url or result.url }}</span>
                    </div>
                    
                    <div class="result-title">
                        <a href="{{ result.url }}" target="_blank">{{ result.title }}</a>
                    </div>
                    
                    <div class="result-content">{{ result.content }}</div>
                    
                    <div class="result-meta">
                        {% if result.engine %}
                        {% set engine_lower = result.engine|lower|replace(' ', '')|replace('images', '')|replace('videos', '') %}
                        <span class="tag engine engine-{{ engine_lower }}">{{ result.engine }}</span>
                        {% endif %}
                        
                        {% set details = result._score_details or {} %}
                        <div class="score-tooltip">
                            <span class="tag score">score:{{ result._score }}</span>
                            <div class="tooltip-content">
                                <div class="tooltip-header">[ SCORE BREAKDOWN ]</div>
                                <div class="tooltip-row">
                                    <span class="tooltip-label">position #{{ details.position }}</span>
                                    <span class="tooltip-value positive">+{{ details.position_score }}</span>
                                </div>
                                {% if details.year %}
                                <div class="tooltip-row">
                                    <span class="tooltip-label">year {{ details.year }}</span>
                                    <span class="tooltip-value {{ 'positive' if details.year_score > 0 else ('negative' if details.year_score < 0 else 'neutral') }}">{{ '%+d' % details.year_score if details.year_score else '0' }}</span>
                                </div>
                                {% endif %}
                                {% if details.factuality_score %}
                                <div class="tooltip-row">
                                    <span class="tooltip-label">factuality</span>
                                    <span class="tooltip-value {{ 'positive' if details.factuality_score > 0 else 'negative' }}">{{ '%+d' % details.factuality_score }}</span>
                                </div>
                                {% endif %}
                                {% if details.language %}
                                <div class="tooltip-row">
                                    <span class="tooltip-label">language ({{ details.language }})</span>
                                    <span class="tooltip-value {{ 'positive' if details.language_score > 0 else ('negative' if details.language_score < 0 else 'neutral') }}">{{ '%+d' % details.language_score if details.language_score else '0' }}</span>
                                </div>
                                {% endif %}
                                {% if details.engine_score %}
                                <div class="tooltip-row">
                                    <span class="tooltip-label">engine penalty</span>
                                    <span class="tooltip-value negative">{{ '%+d' % details.engine_score }}</span>
                                </div>
                                {% endif %}
                                <div class="tooltip-row tooltip-total">
                                    <span>TOTAL</span>
                                    <span>{{ result._score }}</span>
                                </div>
                            </div>
                        </div>
                        
                        {% if details.year %}
                        <span class="tag year">{{ details.year }}</span>
                        {% endif %}
                        
                        {% if details.language %}
                        <span class="tag lang">{{ details.language }}</span>
                        {% endif %}
                    </div>
                </div>
            </div>
            {% endfor %}
            {% endif %}
            
            <!-- Pagination -->
            <div class="pagination">
                {% if page > 1 %}
                <a href="/search?q={{ query }}&tab={{ current_tab }}&pageno={{ page - 1 }}" class="page-btn">[prev]</a>
                {% endif %}
                <span class="page-info">page {{ page }} | {{ results|length }} results</span>
                <a href="/search?q={{ query }}&tab={{ current_tab }}&pageno={{ page + 1 }}" class="page-btn">[next]</a>
            </div>
            
        {% else %}
            <div class="no-results">
                No results found for "{{ query }}"
            </div>
        {% endif %}
    </div>
    {% endif %}
    
    <script>
    (function() {
        let debounceTimer;
        let selectedIndex = -1;
        let suggestions = [];
        
        function setupAutocomplete(inputId, dropdownId) {
            const input = document.getElementById(inputId);
            const dropdown = document.getElementById(dropdownId);
            if (!input || !dropdown) return;
            
            input.addEventListener('input', function() {
                clearTimeout(debounceTimer);
                const query = this.value.trim();
                
                if (query.length < 2) {
                    dropdown.classList.remove('active');
                    suggestions = [];
                    selectedIndex = -1;
                    return;
                }
                
                debounceTimer = setTimeout(() => {
                    fetch('/autocomplete?q=' + encodeURIComponent(query))
                        .then(r => r.json())
                        .then(data => {
                            suggestions = data.slice(0, 8);
                            selectedIndex = -1;
                            
                            if (suggestions.length === 0) {
                                dropdown.classList.remove('active');
                                return;
                            }
                            
                            dropdown.innerHTML = suggestions.map((s, i) => 
                                `<div class="autocomplete-item" data-index="${i}">${escapeHtml(s)}</div>`
                            ).join('');
                            dropdown.classList.add('active');
                            
                            dropdown.querySelectorAll('.autocomplete-item').forEach(item => {
                                item.addEventListener('click', function() {
                                    input.value = suggestions[this.dataset.index];
                                    dropdown.classList.remove('active');
                                    input.form.submit();
                                });
                            });
                        })
                        .catch(() => dropdown.classList.remove('active'));
                }, 150);
            });
            
            input.addEventListener('keydown', function(e) {
                if (!dropdown.classList.contains('active')) return;
                
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    selectedIndex = Math.min(selectedIndex + 1, suggestions.length - 1);
                    updateSelection(dropdown);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    selectedIndex = Math.max(selectedIndex - 1, -1);
                    updateSelection(dropdown);
                } else if (e.key === 'Enter' && selectedIndex >= 0) {
                    e.preventDefault();
                    input.value = suggestions[selectedIndex];
                    dropdown.classList.remove('active');
                    input.form.submit();
                } else if (e.key === 'Escape') {
                    dropdown.classList.remove('active');
                    selectedIndex = -1;
                }
            });
            
            document.addEventListener('click', function(e) {
                if (!input.contains(e.target) && !dropdown.contains(e.target)) {
                    dropdown.classList.remove('active');
                }
            });
        }
        
        function updateSelection(dropdown) {
            dropdown.querySelectorAll('.autocomplete-item').forEach((item, i) => {
                item.classList.toggle('selected', i === selectedIndex);
            });
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Initialize autocomplete for both search boxes
        setupAutocomplete('search-home', 'autocomplete-home');
        setupAutocomplete('search-results', 'autocomplete-results');
    })();
    </script>
</body>
</html>
'''


@app.route('/')
def home():
    """Render the search homepage."""
    return render_template_string(HTML_TEMPLATE, query=None, results=None, show_legend=True, tabs=SEARCH_TABS, current_tab='general')


@app.route('/search')
def search():
    """Proxy search to SearXNG and re-rank results."""
    query = request.args.get('q', '')
    page = request.args.get('pageno', 1, type=int)
    current_tab = request.args.get('tab', 'general')
    
    if not query:
        return render_template_string(HTML_TEMPLATE, query=None, results=None, show_legend=True, page=1, tabs=SEARCH_TABS, current_tab='general')
    
    # Find the tab configuration
    tab_config = next((t for t in SEARCH_TABS if t['id'] == current_tab), SEARCH_TABS[0])
    
    # Fetch knowledge snippet (only on first page for general tab)
    knowledge = None
    if page == 1 and current_tab == 'general':
        knowledge = get_knowledge_snippet(query)
    
    try:
        # Query SearXNG's JSON API
        params = {
            'q': query,
            'format': 'json',
            'pageno': page,
        }
        
        # Add category if specified
        if tab_config.get('categories'):
            params['categories'] = tab_config['categories']
        
        # Add specific engines if specified
        if tab_config.get('engines'):
            params['engines'] = tab_config['engines']
        
        response = requests.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        # Re-rank results
        ranked_results = rank_results(results)
        
        # Limit to RESULTS_PER_PAGE
        ranked_results = ranked_results[:RESULTS_PER_PAGE]
        
        return render_template_string(
            HTML_TEMPLATE, 
            query=query, 
            results=ranked_results, 
            show_legend=False, 
            page=page,
            tabs=SEARCH_TABS,
            current_tab=current_tab,
            knowledge=knowledge
        )
    
    except requests.RequestException as e:
        return render_template_string(
            HTML_TEMPLATE, 
            query=query, 
            results=None, 
            show_legend=False,
            page=page,
            tabs=SEARCH_TABS,
            current_tab=current_tab,
            knowledge=knowledge,
            error=str(e)
        )


@app.route('/api/search')
def api_search():
    """JSON API endpoint for search."""
    query = request.args.get('q', '')
    page = request.args.get('pageno', 1, type=int)
    
    if not query:
        return jsonify({'error': 'No query provided'}), 400
    
    try:
        params = {
            'q': query,
            'format': 'json',
            'pageno': page,
        }
        
        response = requests.get(f"{SEARXNG_URL}/search", params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        results = data.get('results', [])
        
        ranked_results = rank_results(results)
        
        return jsonify({
            'query': query,
            'page': page,
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
        'torrent_engines': TORRENT_ENGINES,
    })


@app.route('/autocomplete')
def autocomplete():
    """Proxy autocomplete requests to SearXNG."""
    query = request.args.get('q', '')
    
    if not query or len(query) < 2:
        return jsonify([])
    
    try:
        response = requests.get(
            f"{SEARXNG_URL}/autocompleter",
            params={'q': query},
            timeout=3
        )
        response.raise_for_status()
        data = response.json()
        # OpenSearch format: ["query", ["suggestion1", "suggestion2", ...]]
        if isinstance(data, list) and len(data) >= 2:
            return jsonify(data[1])
        return jsonify([])
    except requests.RequestException:
        return jsonify([])


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)


"""
Name variant mappings for player name matching.

This includes:
- Common nicknames (e.g., Kostas → Konstantinos)
- Common typos/misspellings (e.g., Gianis → Giannis)
- Transliteration variants (e.g., Yianis → Giannis)

All keys and values should be lowercase.
Add new variants as you discover them in real usage.
"""

NICKNAME_MAP = {
    # Greek nicknames (various spellings and transliterations)
    "kostas": "konstantinos",
    "kotsas": "konstantinos",    # Alternative spelling
    "kostaras": "konstantinos",
    "kostis": "konstantinos",
    "konsta": "konstantinos",
    
    # Giannis variants (typos and transliterations)
    "gianis": "giannis",      # Missing 'n' typo
    "yianis": "giannis",      # Greek transliteration variant
    "gannis": "giannis",      # Different transliteration
    
    # Common English nicknames
    "ken": "kenneth",
    "kenny": "kenneth",
    "bob": "robert",
    "bobby": "robert",
    "bill": "william",
    "billy": "william",
    "mike": "michael",
    "mikey": "michael",
    "chris": "christopher",
    "tony": "anthony",
    "joe": "joseph",
    "joey": "joseph",
    "dan": "daniel",
    "danny": "daniel",
    "dave": "david",
    "matt": "matthew",
    "matty": "matthew",
    "steve": "steven",
    "stevie": "steven",
    "jim": "james",
    "jimmy": "james",
    "tom": "thomas",
    "tommy": "thomas",
    "will": "william",
    "willie": "william",
    
    # European name variants
    "luca": "luka",          # Common typo for Luka Dončić
    
    # Add more variants here as needed:
    # "nickname": "full_name",
}

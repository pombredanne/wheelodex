from enum import Enum

Flags = Enum(
    'Flags', '''
SPACE_SEPARATED_KEYWORDS COMMA_SEPARATED_KEYWORDS
DESCRIPTION_IN_BODY DESCRIPTION_IN_HEADERS CONFLICTING_DESCRIPTIONS
''')
"""Is this a real SDTM variable name?

A model can propose a plausible-looking name that SDTM does not have
(`CMONGO`, `CMDOS`). Drawn on an aCRF it looks authoritative and is wrong, so
proposals are checked against the SDTMIG naming system before they are used:

* variables that carry no domain prefix (`STUDYID`, `VISITNUM`, `QVAL`),
* Demographics' own unprefixed names (`SEX`, `RACE`, `BRTHDTC`),
* everything else is the domain code followed by a standard suffix
  (`AE` + `STDTC`, `LB` + `NRIND`, `CM` + `OCCUR`).

This is the SDTMIG 3.4 variable naming, not a list of any one study's
variables, so it holds for any CRF.
"""

from __future__ import annotations

from typing import Final

UNPREFIXED: Final = frozenset({
    "STUDYID", "DOMAIN", "USUBJID", "POOLID", "SPDEVID", "VISITNUM", "VISIT", "VISITDY", "TAETORD",
    "EPOCH", "RDOMAIN", "IDVAR", "IDVARVAL", "QNAM", "QLABEL", "QVAL", "QORIG", "QEVAL", "RELID",
    "RELTYPE", "APID", "FOCID",
})

DEMOGRAPHICS: Final = frozenset({
    "SUBJID", "RFSTDTC", "RFENDTC", "RFXSTDTC", "RFXENDTC", "RFICDTC", "RFPENDTC", "DTHDTC", "DTHFL",
    "SITEID", "INVID", "INVNAM", "BRTHDTC", "AGE", "AGEU", "SEX", "RACE", "ETHNIC", "ARMCD", "ARM",
    "ACTARMCD", "ACTARM", "ARMNRS", "ACTARMUD", "COUNTRY", "DMDTC", "DMDY",
})

# The parts after the two-letter domain code (AE+TERM, LB+ORRES ...).
SUFFIXES: Final = frozenset({
    # identifiers and grouping
    "SEQ", "GRPID", "REFID", "SPID", "LNKID", "LNKGRP",
    # topic and qualifiers
    "TERM", "MODIFY", "LLT", "LLTCD", "DECOD", "PTCD", "HLT", "HLTCD", "HLGT", "HLGTCD", "CAT", "SCAT",
    "PRESP", "OCCUR", "REASOC", "STAT", "REASND", "BODSYS", "BDSYCD", "SOC", "SOCCD", "LOC", "LAT", "DIR",
    "PORTOT", "METHOD", "EVAL", "EVALID", "ACPTFL", "TRT", "INDC", "CLAS", "CLASCD", "LOT",
    # events
    "SEV", "SER", "ACN", "ACNOTH", "ACNDEV", "REL", "RELNST", "PATT", "OUT", "SCAN", "SCONG", "SDISAB",
    "SDTH", "SHOSP", "SLIFE", "SOD", "SMIE", "CONTRT", "TOX", "TOXGR",
    # interventions
    "DOSE", "DOSTXT", "DOSU", "DOSFRM", "DOSFRQ", "DOSTOT", "DOSRGM", "ROUTE", "ADJ", "PSTRG", "PSTRGU",
    # timing
    "DTC", "STDTC", "ENDTC", "DY", "STDY", "ENDY", "DUR", "TPT", "TPTNUM", "ELTM", "TPTREF", "RFTDTC",
    "STRF", "ENRF", "EVLINT", "EVINTX", "STRTPT", "STTPT", "ENRTPT", "ENTPT", "USCHFL",
    # findings
    "TESTCD", "TEST", "TSTDTL", "ORRES", "ORRESU", "ORNRLO", "ORNRHI", "STRESC", "STRESN", "STRESU",
    "STNRLO", "STNRHI", "STNRC", "NRIND", "RESCAT", "XFN", "NAM", "SPEC", "ANTREG", "SPCCND", "FAST",
    "DRVFL", "CLSIG", "LOBXFL", "BLFL", "POS", "CSTATE", "LEAD", "CHRON", "REPNUM", "LOINC", "ORREF",
    "STREFC", "STREFN", "ANMETH", "RESSCL", "RESTYP", "VAMT", "VAMTU",
    # comments (COVAL, COREF), trial-design style parameters
    "VAL", "REF", "PARMCD", "PARM",
})


def is_standard_variable(domain: str, variable: str) -> bool:
    """True when `variable` is a name SDTM defines for `domain`."""
    domain, variable = str(domain or "").upper(), str(variable or "").upper()
    if variable in UNPREFIXED:
        return True
    if domain == "DM" and variable in DEMOGRAPHICS:
        return True
    if len(domain) != 2 or not variable.startswith(domain):
        return False
    return variable[2:] in SUFFIXES

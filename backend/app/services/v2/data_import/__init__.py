"""Bring any data into the platform as an ontology: tables, event streams and files become object types, objects and links.

The importer is generic: it profiles what it is given (types, keys, links), writes the ontology the same way a reviewed Auto Ontology
candidate set is written (so the revision, the evidence and the lineage are the platform's own), then installs the objects and the
relationship instances. A bundle only says where its tables come from; nothing in here knows about any one scenario.
"""

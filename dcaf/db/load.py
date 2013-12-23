"""
Functions to load data into the dcaf database.

- taxa
- genes
- GO term definitions and gene annotations
- Brenda Tissue Ontology 
  - term definitions 
  - manually annotated GEO accessions (latter not tested)
- GEO SOFT files
- MEDLINE XML
- ORD (not implemented)
- Assembly info from UCSC (not implemented)
"""

import os
import itertools
import functools

import numpy

from plumbum.cmd import tar, grep, cut, zcat, awk, sed, head
from sqlalchemy.exc import IntegrityError

import dcaf.io

from .model import *
from .core import log, get_session
from ..io import MedlineXMLFile, generic_open as open

NCBI_BASE = "ftp://ftp.ncbi.nlm.nih.gov"
GO_OBO = "http://www.geneontology.org/ontology/obo_format_1_2/gene_ontology_ext.obo"

def import_brenda(session):
    """
    Import the BRENDA Tissue Ontology.
    """
    log.info("Importing the BRENDA Tissue Ontology")
    import_obo(session, dcaf.io.data("brenda.obo"), 
               "BTO", "Brenda Tissue Ontology")
 
def import_go(session):
    """
    Import the Gene Ontology and gene-GO annotations.
    """
    log.info("Importing the Gene Ontology")
    import_obo(session, GO_OBO, "GO", "Gene Ontology")

    accessions = {}
    for t in session.query(Ontology).filter_by(namespace="GO").first().terms:
        accessions[t.accession] = t.id

    log.info("Importing GO-gene annotations")
    with open(NCBI_BASE+"/gene/DATA/gene2go.gz") as h:
        h.__next__()
        for line in h:
            fields = line.split("\t")
            term_id = accessions.get(fields[2])
            if term_id and (fields[3] != "IEA"):
                session.add(GeneTerm(gene_id=int(fields[1]),
                                     term_id=term_id,
                                     probability=1))
    session.commit()

def _import_ursa_annotations(session):
    """
    Import the manual GSM tissue annotations from Troyanska et al's URSA.
    """
    term_names = {}
    for t in session.query(Ontology).filter_by(namespace="BTO").first().terms:
        term_names[t.name] = t.id

    sample_accessions = dict(session.query(Sample).select([Sample.c.accession, 
                                                           Sample.c.id]))

    with open(dcaf.io.data("manual_annotations_ursa.csv")) as h:
        h.__next__()
        for line in h:
            fields = line.strip().split('\t')
            gsm = fields[0]
            for term_name in fields[2].split(', '):
                term_id = names.get(term_name)
                sample_id = sample_accessions.get(gsm)
                if term_id and sample_id:
                    session.add(SampleTerm(sample_id=sample_id, 
                                           term_id=term_id,
                                           probability=1))

def initialize_db(session):
    log.info("Initializing DCAF database")
    raw_db = session.connection().engine.raw_connection().connection

    # Add some built-in terms
    log.info("Adding core ontology terms")
    ontology = Ontology(namespace="CORE",
                        description="Core relations inherent to OBO format.")
    session.add(ontology)
    session.commit()

    session.add(Term(ontology_id=ontology.id, 
                     name="is_a"))
    session.add(Term(ontology_id=ontology.id, 
                     name="part_of"))
    session.commit()

    # Load NCBI taxa into 'taxon' table
    log.info("Loading NCBI taxa")
    url = NCBI_BASE + "/pub/taxonomy/taxdump.tar.gz"
    path = dcaf.io.download(url, return_path=True)

    c = raw_db.cursor()
    c.copy_from((tar["Oxzf", path, "names.dmp"] | grep["scientific"] \
                 | cut["-f1,3"]).popen().stdout, "taxon")
    raw_db.commit()

    # Load Entrez Gene IDs, names, symbols, etc, into 'gene'
    log.info("Loading NCBI Entrez Gene data")
    path = dcaf.io.download(NCBI_BASE + "/gene/DATA/gene_info.gz", 
                            return_path=True)
    c = raw_db.cursor()
    c.copy_from((zcat[path] | grep["-v", "^#"] | sed["1d"] \
                 | awk['$1 != 6617'] \
                 | awk['$1 != 543399'] \
                 | awk['$1 != 6085'] \
                 | awk['$1 != 145481'] \
                 | awk['BEGIN { IFS="\t"; OFS="\t" } $1 != 366646 { print $2,$1,$3,$12 }'] \
             ).popen().stdout, "gene")
    raw_db.commit()
    session.commit()

def import_obo(session, path, namespace, description):
    """
    Import an Open Biomedical Ontology (OBO) file
    """
    with open(path) as handle:
        session.query(Ontology).filter(Ontology.namespace==namespace).delete()
        ontology = Ontology(namespace=namespace, description=description)
        session.add(ontology)

        is_a = session.query(Term).filter_by(name="is_a").first()
        part_of = session.query(Term).filter_by(name="part_of").first()
        relations = []

        obo = dcaf.io.OBOFile(handle)
        for term in obo:
            ontology.terms.append(Term(accession=term.id, name=term.name))
            for parent in term.is_a:
                relations.append((term.id, parent, is_a))
            for parent in term.part_of:
                relations.append((term.id, parent, part_of))

        for child_acc, parent_acc, rel in relations:
            child = session.query(Term).filter_by(accession=child_acc).first()
            parent = session.query(Term).filter_by(accession=parent_acc).first()
            session.add(TermRelation(agent=child.id, target=parent.id, 
                                     relation=rel.id, probability=1))
        session.commit()

def import_msigdb(session, path):
    log.info("Importing MSigDB from file: " + path)

    # NOTE: Each GeneSet has an organism (common name) attached.
    # Need to work this in somehow or will the already known Entrez
    # ID-Taxon linkage suffice?
    
    ontology = Ontology(namespace="MSigDB", 
                        description="Broad Institute's Molecular Signature Database")
    session.add(ontology)
    session.flush()
    
    @functools.lru_cache()
    def taxon_symbol_map(taxon_name):
        return dict((g.symbol.lower(), g.id) \
                    for g in session.query(Gene)\
                    .join(Taxon).filter(Taxon.name==taxon_name))

    with dcaf.io.MSigDB(path) as h:
        for entry in h:
            if entry.contributor != "Gene Ontology":
                t = Term(name=entry.name, description=entry.description,
                         accession="MSigDB:"+entry.accession)
                ontology.terms.append(t)
                # FIXME: way to avoid all this committing? (to insert
                # only if gene ID exists?)
                session.flush()
                symbol_map = taxon_symbol_map(entry.organism)
                for symbol in entry.gene_symbols:
                    gene_id = symbol_map.get(symbol.lower())
                    if gene_id:
                        link = GeneTerm(term_id=t.id, gene_id=gene_id, probability=1)
                        session.merge(link)
    session.commit()

def import_soft(session, path):
    """
    Load a GEO SOFT file (mostly expression data) into the database.
    """

    # FIXME: Make a better API
    parser = dcaf.io.SOFTParser(path)

    taxon_id = parser._taxon_id
    platform_accession = parser._platform_accession
    
    platform = Platform(taxon_id=taxon_id, accession=platform_accession)
    session.add(platform)

    q = session.query(Gene).filter_by(taxon_id=taxon_id).with_entities(Gene.id)
    genes = sorted(itertools.chain(*q))

    for i,sample in enumerate(parser):
        v = sample["expression"]
        v = dict(zip(v.index, v))
        data = [v.get(g, numpy.nan) for g in genes]
        del sample["expression"]
        sample["data"] = data
        sample["platform_id"] = platform.id
        session.add(Sample(**sample))
        if (i % 100) == 0:
            session.commit()
            print("*", i, "records loaded.")
    session.commit()

def import_medline(session, path):
    """
    Load MEDLINE XML files into the database.

    If ``path`` is a file, load that single file; if it is a folder, 
    search (nonrecursively) that folder for MEDLINE XML files, then load
    each one.
    """
    if not os.path.exists(path):
        raise Exception("Path '%s' does not exist!" % path)
    if os.path.isfile(path):
        raise Exception("Single file import not implemented!")
    folder = path

    journal_ids = set()

    for file in os.listdir(folder):
        if file.endswith(".xml.gz"):
            path = os.path.join(folder, file)
            log.info("Importing MEDLINE articles from: %s" % path)
            with MedlineXMLFile(path) as handle:
                for article in handle:
                    journal = article.journal
                    if journal.id not in journal_ids:
                        session.add(Journal(
                            id=journal.id,
                            issn=journal.issn, 
                            title=journal.name))
                        journal_ids.add(journal.id)

                    session.merge(Article(
                        journal_id=journal.id,
                        id=article.id,
                        publication_date=article.publication_date,
                        title=article.title,
                        abstract=article.abstract))
            session.commit()
    session.commit()


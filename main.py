"""
uBuchhaltung - Simple Accounting Software
Entry point for the web server
"""
from document_parser import DocumentParser
from server import run_server

if __name__ == "__main__":
    parser = DocumentParser()   # Init, if Log not exists, create it
    run_server()

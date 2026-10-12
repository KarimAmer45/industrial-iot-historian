import pathlib,sys
import pytest
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'ingest'),str(ROOT/'scripts')]
from common import connect
@pytest.fixture
def db():
    c=connect('pytest')
    try:yield c
    finally:c.rollback();c.close()

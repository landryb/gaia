#!/bin/env python3
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et
from celery.utils.nodenames import host_format
from kombu.transport.virtual.base import logger
from sqlalchemy import create_engine, MetaData, inspect, select, or_, and_
from sqlalchemy.engine import URL
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.exc import NoResultFound,OperationalError
from sqlalchemy.orm import sessionmaker
import json
import requests
from os import getenv
import re

from owslib.wms import WebMapService
from owslib.wfs import WebFeatureService
from owslib.wmts import WebMapTileService
from owslib.util import ServiceException

from flask import current_app as app
from celery import shared_task
from celery import Task
from celery import group
from celery.utils.log import get_task_logger
from sqlalchemy.testing.suite.test_reflection import users

from geordash.logwrap import get_logger
from geordash.utils import objtype


# solves conflicts in relationship naming ?
def name_for_collection_relationship(base, local_cls, referred_cls, constraint):
    name = referred_cls.__name__.lower()
    local_table = local_cls.__table__
    #print("local_cls={}, local_table={}, referred_cls={}, will return name={}, constraint={}".format(local_cls, local_table, referred_cls, name, constraint))
    if name in local_table.columns:
        newname = name + "_"
        print(
            "Already detected name %s present.  using %s" %
            (name, newname))
        return newname
    return name

class MapstoreChecker():
    def __init__(self, conf):

        user = conf.get('pgsqlUser')

        host = conf.get('pgsqlHost')

        port = conf.get('pgsqlPort')

        passwd = conf.get('pgsqlPassword')

        database = conf.get('pgsqlDatabase')

        url = URL.create(
            drivername="postgresql",
            username=user,
            host=host,
            port=port,
            password=passwd,
            database=database
        )

        engine = create_engine(url)#, echo=True, echo_pool="debug")

# these three lines perform the "database reflection" to analyze tables and relationships
        m = MetaData(schema=conf.get('pgsqlGeoStoreSchema','mapstoregeostore'))
        Base = automap_base(metadata=m)
        Base.prepare(autoload_with=engine,name_for_collection_relationship=name_for_collection_relationship)

        # there are many tables in the database but me only directly use those
        self.Resource = Base.classes.gs_resource
        Category = Base.classes.gs_category

        self.sessionm = sessionmaker(bind=engine)
        self.sessiono = self.sessionm()

        categories = self.session().query(Category).all()
        self.cat = dict()
        for c in categories:
            self.cat[c.name] = c.id

    def session(self):
        try:
            self.sessiono.execute(select(1))
        except OperationalError:
            get_logger("CheckMapstore").info("reconnecting to db")
            self.sessiono = self.sessionm()
        return self.sessiono


@shared_task()
def check_configs():
    """ check mapstore configs:
    - catalog entries provided in localConfig.json are valid
    - layers & backgrounds in new/config.json exist in services
    """
    ret = list()
    datadirpath = getenv('georchestradatadir', '/etc/georchestra')
    with open(f"{datadirpath}/mapstore/configs/localConfig.json") as file:
        localconfig = json.load(file)
        catalogs = localconfig["initialState"]["defaultState"]["catalog"]["default"]["services"]
        ret.append({'args': "localconfig", "problems": check_catalogs(catalogs)})

    for filetype in ["new", "config"]:
        with open(f"{datadirpath}/mapstore/configs/{filetype}.json") as file:
            s = file.read()
            mapconfig = json.loads(s)
            layers = mapconfig["map"]["layers"]
            ret.append({'args': filetype, "problems": check_layers(layers, 'MAP', filetype)})
    return ret

def get_res(rescat, resid):
    msc = app.extensions["msc"]
    try:
        r = msc.session().query(msc.Resource).filter(and_(msc.Resource.category_id == msc.cat[rescat], msc.Resource.id == resid)).one()
    except NoResultFound as e:
        get_logger("CheckMapstore").error(f"no such {rescat} with id {resid}")
        return None
    return r

def get_all_res(rescat):
    msc = app.extensions["msc"]
    try:
        r = msc.session().query(msc.Resource).filter(msc.Resource.category_id == msc.cat[rescat]).all()
    except NoResultFound as e:
        get_logger("CheckMapstore").error(f"no {rescat} in database")
        return None
    return r

@shared_task()
def check_res(rescat, resid):
    m = get_res(rescat, resid)
    if not m:
        return {'problems':[{'type': 'NoSuchResource', 'restype': rescat, 'resid': resid }]}
    get_logger("CheckMapstore").info("Checking {} avec id {} ayant pour titre {}".format('la carte' if rescat == 'MAP' else 'le contexte', m.id, m.name))
    ret = dict()
    # uses automapped attribute from relationship instead of a query
    data = json.loads(m.gs_stored_data[0].stored_data)
    ret['problems'] = list()
    layers = list()
    catalogs = list()
    if rescat == 'MAP':
        layers = data["map"]["layers"]
    else:
        if "map" in data["mapConfig"]:
            layers = data["mapConfig"]["map"]["layers"]
    if layers:
        ret['problems'] += check_layers(layers, rescat, resid)

    if rescat == 'MAP':
        catalogs = data["catalogServices"]["services"]
    else:
        if "catalogServices" in data["mapConfig"]:
            catalogs = data["mapConfig"]["catalogServices"]["services"]
    if catalogs:
        ret['problems'] += check_catalogs(catalogs)
    return ret

@shared_task
def check_resources(categories = ['MAP', 'CONTEXT']):
    """
    called by beat scheduler, or check_mapstore_resources() route in views
    """
    if type(categories) != list:
        categories = [categories]
    msc = app.extensions["msc"]
    taskslist = list()
    for rescat in categories:
        res = msc.session().query(msc.Resource).filter(msc.Resource.category_id == msc.cat[rescat]).all()
        for r in res:
            taskslist.append(check_res.s(rescat, r.id))
    grouptask = group(taskslist)
    groupresult = grouptask.apply_async()
    groupresult.save()
    return groupresult

def check_layers(layers, rescat, resid):
    ret = list()
    for l in layers:
        match l['type']:
            case 'wms'|'wfs'|'wmts':
                get_logger("CheckMapstore").info('uses {} layer name {} from {} (id={})'.format(l['type'], l['name'], l['url'], l['id']))
                s = app.extensions["owscache"].get(l['type'], l['url'])
                if s.s is None:
                    ret.append({'type':'OGCException', 'url': l['url'], 'stype': l['type'], 'exception': objtype(s.exception), 'exceptionstr': str(s.exception)})
                else:
                    get_logger("CheckMapstore").debug('checking for layer presence in ows entry with ts {}'.format(s.timestamp))
                    if l['name'] not in s.contents():
                        ret.append({'type':'NoSuchLayer', 'url': l['url'], 'stype': l['type'], 'lname': l['name']})
            case '3dtiles' | 'cog':
                try:
                    response = requests.head(l['url'], allow_redirects=True)
                    if response.status_code != 200:
                        ret.append({'type': 'BrokenDatasetUrl', 'url': l['url'], 'code': response.status_code})
                except Exception as e:
                    ret.append({'type': 'ConnectionFailure', 'url': l['url'], 'exception': objtype(e), 'exceptionstr': str(e)})
            case 'empty':
                pass
            case 'osm':
                pass
            case _:
                get_logger("CheckMapstore").debug(l)
    return ret

def check_catalogs(catalogs):
    ret = list()
    for k,c in catalogs.items():
        get_logger("CheckMapstore").debug(f"checking catalog entry {k} type {c['type']} at {c['url']}")
        match c['type']:
            case 'wms'|'wfs'|'wmts'|'csw':
                s = app.extensions["owscache"].get(c['type'], c['url'])
                if s.s is None:
                    ret.append({'type':'OGCException', 'url': c['url'], 'stype': c['type'], 'exception': objtype(s.exception), 'exceptionstr': str(s.exception)})
            case '3dtiles' | 'cog':
                try:
                    response = requests.head(c['url'], allow_redirects=True)
                    if response.status_code != 200:
                        ret.append({'type': 'BrokenDatasetUrl', 'url': c['url'], 'code': response.status_code})
                except Exception as e:
                    ret.append({'type': 'ConnectionFailure', 'url': c['url'], 'exception': objtype(e), 'exceptionstr': str(e)})
            case _:
                pass
    return ret

def get_resources_using_ows(owstype, url, layer=None):
    """ returns a set of ms resources (tuples with resource type & id)
    using a given ows layer from a given ows service type at a given url
    this awful function builds a reverse full map at each call, because i
    havent found a way to query json inside stored_data from sqlalchemy
    (and mapstore hibernate doesnt use proper psql json types on purpose?)
    if layer is not set, then it will return the set of resources using
    the given service
    """
    msc = app.extensions["msc"]
    if '~' in url:
        url = url.replace('~','/')
    layermap = dict()
    servicemap = dict()
    resources = msc.session().query(msc.Resource).filter(or_(msc.Resource.category_id == msc.cat['MAP'], msc.Resource.category_id == msc.cat['CONTEXT'])).all()
    for r in resources:
        layers = None
        data = json.loads(r.gs_stored_data[0].stored_data)
        if r.category_id == msc.cat['MAP']:
            layers = data["map"]["layers"]
            rcat = 'MAP'
        else:
            # context without map ?
            if "map" not in data["mapConfig"]:
                continue
            layers = data["mapConfig"]["map"]["layers"]
            rcat = 'CONTEXT'
        for l in layers:
            if 'group' in l and l["group"] == 'background':
                continue
            match l['type']:
                case 'wms'|'wfs'|'wmts':
                    lkey = (l['type'], l['url'], l['name'])
                    skey = (l['type'], l['url'])
                    val = (rcat, r.id, r.name)
                    if lkey not in layermap:
                        layermap[lkey] = set()
                    if skey not in servicemap:
                        servicemap[skey] = set()
                    layermap[lkey].add(val)
                    servicemap[skey].add(val)
                case _:
                    pass
    if layer is None:
        return servicemap.get((owstype, url), None)
    return layermap.get((owstype, url, layer), None)

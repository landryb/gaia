#!/bin/env python3
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et

from flask import Blueprint
from flask import request, jsonify, abort
from flask import current_app as app
import requests
import json

api_bp = Blueprint("api", __name__, url_prefix="/api")

def mapstore_get(request, url, accept_json = True):
    headers = { 'sec-proxy': 'true' }
    if accept_json:
        headers['Accept'] = 'application/json'
    if 'sec-username' in request.headers:
        headers['sec-username'] = request.headers.get('Sec-Username')
    if 'sec-roles' in request.headers:
        headers['sec-roles'] = request.headers.get('Sec-Roles')
    msurl = app.extensions["conf"].get('mapstore', 'secproxytargets')
    return requests.get(msurl + url, headers = headers)

@api_bp.route("/mapstore/maps.json")
def maps():
    maps = mapstore_get(request, 'rest/geostore/extjs/search/category/MAP/***/thumbnail,details,featured?includeAttributes=true')
    if maps.status_code != 200:
        app.logger.error(f"failed getting MAPs from geostore, got {maps.status_code}: {maps.text}")
        return str(maps.status_code)
    return maps.json()

@api_bp.route("/mapstore/contexts.json")
def contexts():
    maps = mapstore_get(request, 'rest/geostore/extjs/search/category/CONTEXT/***/thumbnail,details,featured?includeAttributes=true')
    if maps.status_code != 200:
        app.logger.error(f"failed getting CONTEXTs from geostore, got {maps.status_code}: {maps.text}")
        return str(maps.status_code)
    return maps.json()

def gninternalid(request, uuid):
    headers = {'sec-proxy': 'true', 'Content-Type': 'application/json', 'Accept': 'application/json'}
    if 'sec-username' in request.headers:
        headers['sec-username'] = request.headers.get('Sec-Username')
    if 'sec-roles' in request.headers:
        headers['sec-roles'] = request.headers.get('Sec-Roles')
    gnurl = app.extensions["conf"].get(app.extensions["conf"].get('localgn', 'urls'), 'secproxytargets')
    query = { "size": 1,
              "_source": {"includes": ["id"]},
              "query": { "bool": { "must": [ { "query_string" : { "query": "uuid: " + uuid } }, { "terms": { "isTemplate": [ "y", "n" ] } }]}}
    }
    md = requests.post(gnurl + "srv/api/search/records/_search",
        json = query,
        headers = headers)
    if md.status_code != 200:
      return md.text
    rep = md.json()
    if len(rep['hits']['hits']) != 1:
        return None
    return rep['hits']['hits'][0]['_source']['id']

def get_res_details(request, res):
    # gs_attribute is a list coming from the relationship between gs_resource and gs_attribute
    ret = {'attribute': dict(), 'owner': None, 'groups': dict(), 'title': res.name, 'description': res.description}
    for a in res.gs_attribute:
        if a.name in ('owner', 'context', 'details', 'thumbnail'):
            ret['attribute'][a.name] = a.attribute_text
            if a.name == 'details' and a.attribute_text != "NODATA":
                r = mapstore_get(request, a.attribute_text, False)
                if r.status_code == 200:
                    ret['attribute'][a.name] = r.text
    for s in res.gs_security:
        # in the ms2-geor project, an entry with username is the owner
        if s.username is not None:
            ret['owner'] = s.username
        if s.groupname is not None:
            ret['groups'][s.groupname] = { 'canread': s.canread, 'canwrite': s.canwrite }
    return ret

"""
returns preauth cookies for subsequent queries
"""
def geonetwork_preauth(gnurl):
    headers = {'Accept': 'application/json'}
    username = request.headers.get('Sec-Username','anonymous')
    if username != 'anonymous':
        headers['sec-username'] = username
    preauth = requests.get(gnurl + "srv/api/me", headers=headers)
    if preauth.status_code == 204:
      if 'XSRF-TOKEN' in preauth.cookies:
        headers['X-XSRF-TOKEN'] = preauth.cookies['XSRF-TOKEN']
        headers['sec-proxy'] = 'true'
        me = requests.get(gnurl + "srv/api/me",
            cookies = preauth.cookies,
            headers = headers)
        if me.status_code != 200:
            return me.text
      else:
        return f'No XSRF-TOKEN in {preauth.cookies} ?'
    else:
        return preauth.status_code
    return (headers, preauth.cookies, me)

@api_bp.route("/geonetwork/subportals.json")
def geonetwork_subportals():
    gnurl = app.extensions["conf"].get(app.extensions["conf"].get('localgn', 'urls'), 'secproxytargets')
    r = geonetwork_preauth(gnurl)
    if type(r) != tuple:
        return r

    headers = r[0]
    cookies = r[1]
    me = r[2]
    portals = requests.get(gnurl + 'srv/api/sources/subportal',
        cookies = cookies,
        headers = headers)
    if portals.status_code != 200:
        return portals.text
    return portals.json()

@api_bp.route("/geonetwork/metadatas.json")
def metadatas():
    # bail out early if user is not auth
    username = request.headers.get('Sec-Username','anonymous')
    if username == 'anonymous':
        return abort(403)
    def_es_querysize = 60
    url1 = app.extensions["conf"].get('localgn', 'urls')
    gnurl = app.extensions["conf"].get(url1, 'secproxytargets')
    preauth = requests.get(gnurl + "srv/api/me", headers={'Accept': 'application/json'})
    if preauth.status_code == 204:
      if 'XSRF-TOKEN' in preauth.cookies:
        me = requests.get(gnurl + "srv/api/me",
            cookies = preauth.cookies,
            headers = {'Accept': 'application/json', 'sec-proxy': 'true', 'sec-username': username, 'X-XSRF-TOKEN': preauth.cookies['XSRF-TOKEN']})
        if me.status_code != 200:
            return me.text
        else:
            if 'id' in me.json():
                query = { "size": def_es_querysize,
                            "_source": {"includes": ["id", "documentStandard", "resourceTitleObject", "isHarvested", "resourceType" ]},
                            "query": { "bool": { "must": [ { "query_string" : { "query": "owner: {}".format(me.json()['id']) } }, { "terms": { "isTemplate": [ "y", "n" ] } }]}}
                }
                md = requests.post(gnurl + "srv/api/search/records/_search",
                    json = query,
                    cookies = preauth.cookies,
                    headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'sec-proxy': 'true', 'sec-username': username, 'X-XSRF-TOKEN': preauth.cookies['XSRF-TOKEN']})
                if md.status_code != 200:
                    return md.text
                rep = md.json()
                nrec = rep['hits']['total']['value']
                app.logger.debug(f"got {nrec} records where {username} is editor")
                if nrec > def_es_querysize:
                    query["size"] = nrec + 2
                    md = requests.post(gnurl + "srv/api/search/records/_search",
                        json = query,
                        cookies = preauth.cookies,
                        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'sec-proxy': 'true', 'sec-username': username, 'X-XSRF-TOKEN': preauth.cookies['XSRF-TOKEN']})
                    if md.status_code != 200:
                        return md.text
                    rep = md.json()
                retval = list()
                for h in rep['hits']['hits']:
                    try:
                        title = h['_source']['resourceTitleObject']['default']
                    except KeyError as e:
                        app.logger.error(f"no title in md {h['_id']} ? no key {str(e)}")
                        title = "No title in index ?"
                    retval.append({ '_id':h['_id'], 'gnid': h['_source']['id'], 'gaialink': h['isPublishedToAll'] and h['_source']['isHarvested'] != "true", 'title': title })
                return jsonify(retval)
    else:
        return preauth.status_code

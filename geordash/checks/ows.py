#!/bin/env python3
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et

import requests

from celery import shared_task
from celery import Task
from celery import group
from celery.utils.log import get_task_logger
tasklogger = get_task_logger("CheckOws")

from flask import current_app as app
from geordash.utils import find_localmduuid, unmunge

import xml.etree.ElementTree as ET
from owslib.util import ServiceException

def find_tilematrix_center(wmts, lname):
    """
    for a given wmts layer, find the 'center' tile at the last tilematrix level
    and return a tuple with:
    - the last tilematrix level to query
    - the tilematrix name
    - the row/column index at the center of the matrix
    """
    # find first tilematrixset
    # tilematrixset is a service attribute
    tsetk = list(wmts.tilematrixsets.keys())[0]
    tset = wmts.tilematrixsets[tsetk]
    # find last tilematrix level
    tmk = list(tset.tilematrix.keys())[-1]
    lasttilematrix = tset.tilematrix[tmk]
#    print(f"first tilematrixset named {tsetk}: {tset}")
#    print(f"last tilematrix lvl named {tmk}: {lasttilematrix} (type {type(lasttilematrix)}")
#    print(f"width={lasttilematrix.matrixwidth}, height={lasttilematrix.matrixheight}")
    # tilematrixsetlink is a layer attribute
    l = wmts.contents[lname]
    tms = list(l.tilematrixsetlinks.keys())[0]
#    print(f"first tilesetmatrixlink for layer {lname} named {tms}")
    tsetl = l.tilematrixsetlinks[tms]
    #geoserver/gwc sets tilematrixsetlinks, mapproxy doesnt
    if len(tsetl.tilematrixlimits) > 0:
        tmk = list(tsetl.tilematrixlimits.keys())[-1]
        tml = tsetl.tilematrixlimits[tmk]
        r = tml.mintilerow + int((tml.maxtilerow - tml.mintilerow) / 2)
        c = tml.mintilecol + int((tml.maxtilecol - tml.mintilecol) / 2)
    else:
        r = int(int(lasttilematrix.matrixwidth) / 2)
        c = int(int(lasttilematrix.matrixheight) / 2)
    return (tms, tmk, r, c)

def reduced_bbox(bbox):
    """
    for a layer bounding box, return a very small bbox at the center of it
    used for getmap/getfeature tests to ensure it doesn't hammer the remote
    """
    xmin, ymin, xmax, ymax = bbox
    return [xmin+0.49*(xmax-xmin),
         ymin+0.49*(ymax-ymin),
         xmax-0.49*(xmax-xmin),
         ymax-0.49*(ymax-ymin)]

@shared_task()
def owsservice(stype, url):
    service = app.extensions["owscache"].get(stype, url)
    if service.s is None:
        return False
    taskslist = list()
    for lname in service.contents():
        taskslist.append(owslayer.s(stype, url, lname))
    grouptask = group(taskslist)
    groupresult = grouptask.apply_async()
    groupresult.save()
    return groupresult

@shared_task()
def owslayer(stype, url, layername):
    """
    Given an ows layer check that:
    - it refers to existing metadata ids
    - a getmap/getfeature/gettile query succeeds
    :param stype: the service type (wms/wfs/wmts)
    :param url: the service url
    :param layername: the layer name in the service object
    :return: the list of errors
    """
    tasklogger.info(f"checking layer {layername} in {stype} {url}")
    ret = dict()
    ret['problems'] = list()
    url = unmunge(url)
    service = app.extensions["owscache"].get(stype, url)
    l = service.contents()[layername]
    if hasattr(l, 'metadataUrls'):
        for m in l.metadataUrls:
            mdurl = m['url']
            # check first that the url exists
            r = requests.head(mdurl)
            if r.status_code != 200:
                ret['problems'].append(f"metadataurl at {mdurl} doesn't seem to exist (returned code {r.status_code})")
            tasklogger.debug(f"{mdurl} -> {r.status_code}")
        if len(l.metadataUrls) == 0:
            ret['problems'].append(f"{layername} has no metadataurl")

    localmduuids = find_localmduuid(service.s, layername)
    # in a second time, make sure local md uuids are reachable via csw
    if len(localmduuids) > 0:
        localgn = app.extensions["conf"].get('localgn', 'urls')
        cswservice = app.extensions["owscache"].get('csw', '/' + localgn + '/srv/fre/csw')
        csw = cswservice.s
        try:
            csw.getrecordbyid(list(localmduuids))
        except Exception as e:
            tasklogger.error(f"exception {str(e)} on getrecordbyid({list(localmduuids)})")
        else:
            tasklogger.debug(csw.records)
            for uuid in localmduuids:
                if uuid not in csw.records:
                    ret['problems'].append(f"md with uuid {uuid} not found in local csw")
                else:
                    tasklogger.debug(f"md with uuid {uuid} exists, title {csw.records[uuid].title}")

    operation = ""
    try:
        if stype == "wms":
            operation = "GetMap"
            if operation not in [op.name for op in service.s.operations]:
                ret['problems'].append(f"{operation} unavailable")
                return ret
            defformat = service.s.getOperationByName('GetMap').formatOptions[0]
            r = service.s.getmap(layers=[layername],
                srs='EPSG:4326',
                format=defformat,
                size=(10,10),
                bbox=reduced_bbox(l.boundingBoxWGS84))
            headers = r.info()
            if headers['content-type'] != defformat: # and headers['content-type'] != 'image/jpeg':
                ret['problems'].append(f"{operation} succeded but returned format {headers['content-type']} didn't match expected {defformat}")
            # content-length only available for HEAD requests ?
            if 'content-length' in headers and not int(headers['content-length']) > 0:
                ret['problems'].append(f"{operation} succeded but the result size was {headers['content-length']}")

        elif stype == "wfs":
            operation = "GetFeature"
            feat = service.s.getfeature(typename=[layername],
                srsname=l.crsOptions[0],
#                bbox=reduced_bbox(l.boundingBoxWGS84),
                maxfeatures=1)
            xml = feat.read()
            try:
                root = ET.fromstring(xml.decode())
                first_tag = root.tag.lower()
                if not first_tag.endswith("featurecollection"):
                    ret['problems'].append(f"{operation} succeeded but the first XML tag of the response was {first_tag}")
            except lxml.etree.XMLSyntaxError as e:
                ret['problems'].append(f"{operation} succeeded but didnt return XML ? {xml.decode()}")

        elif stype == "wmts":
            operation = "GetTile"
            (tms, tm, r, c) = find_tilematrix_center(service.s, layername)
            tile = service.s.gettile(layer=layername, tilematrixset = tms, tilematrix = tm, row = r, column = c)
            headers = tile.info()
            if headers['content-type'] != l.formats[0]:
                ret['problems'].append(f"{operation} succeded but returned format {headers['content-type']} didn't match expected {l.formats[0]}")
            if 'content-length' in headers and not int(headers['content-length']) > 0:
                ret['problems'].append(f"{operation} succeded but the result size was {headers['content-length']}")

    except ServiceException as e:
        if type(e.args) == tuple and "interdit" in e.args[0]:
            ret['problems'].append(f"got a 403 for {operation} on {layername} in {stype} at {url}")
        else:
            ret['problems'].append(f"failed {operation} on {layername} in {stype} at {url}: {e}")
    else:
       tasklogger.debug(f"{operation} on {layername} in {stype} at {url} succeeded")
    return ret

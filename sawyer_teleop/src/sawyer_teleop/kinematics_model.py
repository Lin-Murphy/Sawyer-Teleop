"""Compact, versioned URDF chain shared with A. Standard library only on B."""
import hashlib
import json
import math
import xml.etree.ElementTree as ET

from .protocol import JOINTS, vector


def model_digest(model):
    payload = {k: v for k, v in model.items() if k not in ('sha256', 'available', 'urdf_sha256')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def read_kinematics_model(urdf, tip_frame='right_hand', base_frame='base'):
    """Export only the active chain; meshes, ROS plugins and unrelated joints stay on B."""
    try:
        root = ET.fromstring(urdf)
        children = {}
        for joint in root.findall('joint'):
            child = joint.find('child').get('link')
            if child in children:
                raise ValueError('Duplicate model child')
            children[child] = joint
        chain, seen, link = [], set(), tip_frame
        while link != base_frame:
            if link in seen or len(seen) >= 32:
                raise ValueError('Invalid model chain')
            seen.add(link)
            joint = children[link]
            kind, name = joint.get('type'), joint.get('name')
            if kind not in ('fixed', 'revolute') or joint.find('mimic') is not None:
                raise ValueError('Unsupported joint in active chain: '+str(name))
            origin = joint.find('origin')
            def numbers(element, key, default):
                return vector([float(x) for x in (element.get(key, default) if element is not None else default).split()], 3)
            item = dict(name=name, type=kind, xyz=numbers(origin, 'xyz', '0 0 0'),
                        rpy=numbers(origin, 'rpy', '0 0 0'))
            if kind == 'revolute':
                axis = numbers(joint.find('axis'), 'xyz', '1 0 0')
                norm = math.sqrt(sum(x*x for x in axis))
                if norm < 1e-10:
                    raise ValueError('Invalid joint axis')
                item['axis'] = [x/norm for x in axis]
                limit = joint.find('limit')
                lo, hi = float(limit.get('lower')), float(limit.get('upper'))
                if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
                    raise ValueError('Invalid joint angle limits')
                item.update(lower=lo, upper=hi)
            chain.insert(0, item)
            link = joint.find('parent').get('link')
        if [x['name'] for x in chain if x['type'] != 'fixed'] != JOINTS:
            raise ValueError('Active chain does not match Sawyer joint order')
        result = dict(available=True, schema=1, base_frame=base_frame, tip_frame=tip_frame,
                      joints=list(JOINTS), chain=chain)
        result['sha256'] = model_digest(result)
        result['urdf_sha256'] = hashlib.sha256(urdf.encode()).hexdigest()
        return result
    except (ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
        return dict(available=False, reason='Kinematic model unavailable: '+str(exc))

'''
This file of part of CalcUS.

Copyright (C) 2020-2022 Raphaël Robidas

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
'''


import os
import glob
import random
import string
import bleach
import math
import time
import zipfile
from os.path import basename
from io import BytesIO
import basis_set_exchange
import numpy as np
import ccinput

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from django.shortcuts import render, redirect
from django.http import HttpResponse, HttpResponseRedirect
from django.views import generic
from django.utils import timezone
from django.core.files.storage import FileSystemStorage
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.auth import login, update_session_auth_hash
from django.utils.datastructures import MultiValueDictKeyError
from django.db.models import Prefetch
from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm

from .forms import UserCreateForm
from .models import Calculation, Profile, Project, ClusterAccess, Example, PIRequest, ResearchGroup, Parameters, Structure, Ensemble, BasicStep, CalculationOrder, Molecule, Property, Filter, Preset, Recipe, Folder, CalculationFrame
from .tasks import dispatcher, del_project, del_molecule, del_ensemble, del_order, BASICSTEP_TABLE, SPECIAL_FUNCTIONALS, cancel, run_calc, send_cluster_command
from .decorators import superuser_required
from .tasks import system, analyse_opt, generate_xyz_structure, gen_fingerprint, get_Gaussian_xyz
from .constants import *
from .libxyz import parse_xyz_from_text, equivalent_atoms
from .environment_variables import *
from .calculation_helper import get_xyz_from_Gaussian_input

from shutil import copyfile, make_archive, rmtree
from django.db.models.functions import Lower
from django.conf import settings

from throttle.decorators import throttle

#import nmrglue as ng

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]  %(module)s: %(message)s")
logger = logging.getLogger(__name__)

class IndexView(generic.ListView):
    template_name = 'frontend/dynamic/list.html'
    context_object_name = 'latest_frontend'
    paginate_by = '20'

    def get_queryset(self, *args, **kwargs):
        if isinstance(self.request.user, AnonymousUser):
            return []

        try:
            page = int(self.request.GET.get('page'))
        except KeyError:
            page = 0

        self.request.session['previous_page'] = page
        proj = clean(self.request.GET.get('project'))
        type = clean(self.request.GET.get('type'))
        status = clean(self.request.GET.get('status'))
        target_username = clean(self.request.GET.get('user'))
        mode = clean(self.request.GET.get('mode'))

        try:
            target_profile = User.objects.get(username=target_username).profile
        except User.DoesNotExist:
            return []

        if profile_intersection(self.request.user.profile, target_profile):
            if mode in ["Workspace", "Unseen only"]:
                hits = target_profile.calculationorder_set.filter(hidden=False)
            elif mode == "All orders":
                hits = target_profile.calculationorder_set.all()

            if proj != "All projects":
                hits = hits.filter(project__name=proj)
            if type != "All steps":
                hits = hits.filter(step__name=type)
            if status != "All statuses":
                new_hits = []
                for hit in hits:
                    if hit.status == Calculation.CALC_STATUSES[status]:
                        new_hits.append(hit)
                hits = new_hits
            if mode == "Unseen only":
                new_hits = []
                for hit in hits:
                    if hit.status != hit.last_seen_status:
                        new_hits.append(hit)
                hits = new_hits

            res = sorted(hits, key=lambda d: (1 if d.new_status or d.status == 1 else 0, d.date), reverse=True)
            return res
        else:
            return []

def home(request):
    return render(request, 'frontend/home.html')

@login_required
def periodictable(request):
    return render(request, 'frontend/dynamic/periodictable.html')

@login_required
def specifications(request):
    return render(request, 'frontend/dynamic/specifications.html')

@login_required
def get_available_bs(request):
    if 'elements' in request.POST.keys():
        raw_el = clean(request.POST['elements'])
        elements = [i.strip() for i in raw_el.split(' ') if i.strip() != '']
    else:
        elements = None

    basis_sets = basis_set_exchange.filter_basis_sets(elements=elements)
    response = ""
    for k in basis_sets.keys():
        name = basis_sets[k]['display_name']
        response += """<option value="{}">{}</option>\n""".format(k, name)
    return HttpResponse(response)

@login_required
def get_available_elements(request):
    if 'bs' in request.POST.keys():
        bs = clean(request.POST['bs'])
    else:
        return HttpResponse(status=204)

    md = basis_set_exchange.get_metadata()
    if bs not in md.keys():
        return HttpResponse(status=404)

    version = md[bs]['latest_version']
    elements = md[bs]['versions'][version]['elements']
    response = ' '.join(elements)
    return HttpResponse(response)

@login_required
def aux_molecule(request):
    if 'proj' not in request.POST.keys():
        return HttpResponse(status=404)

    project = clean(request.POST['proj'])

    if project.strip() == '' or project == 'New Project':
        return HttpResponse(status=204)

    try:
        project_set = request.user.profile.project_set.filter(name=project)
    except Profile.DoesNotExist:
        return HttpResponse(status=404)

    if len(project_set) != 1:
        logger.warning("More than one project with the same name found!")
        return HttpResponse(status=404)
    else:
        project_obj = project_set[0]

    return render(request, 'frontend/dynamic/aux_molecule.html', {'molecules': project_obj.molecule_set.all()})

@login_required
def aux_ensemble(request):
    if 'mol_id' not in request.POST.keys():
        return HttpResponse(status=404)

    _id = clean(request.POST['mol_id'])
    if _id.strip() == '':
        return HttpResponse(status=204)
    id = int(_id)

    try:
        mol = Molecule.objects.get(pk=id)
    except Molecule.DoesNotExist:
        return HttpResponse(status=404)

    if not can_view_molecule(mol, request.user.profile):
        return HttpResponse(status=404)

    return render(request, 'frontend/dynamic/aux_ensembles.html', {'ensembles': mol.ensemble_set.all()})

@login_required
def aux_structure(request):
    if 'e_id' not in request.POST.keys():
        return HttpResponse(status=404)

    _id = clean(request.POST['e_id'])
    if _id.strip() == '':
        return HttpResponse(status=204)
    id = int(_id)

    try:
        e = Ensemble.objects.get(pk=id)
    except Ensemble.DoesNotExist:
        return HttpResponse(status=404)

    if not can_view_ensemble(e, request.user.profile):
        return HttpResponse(status=404)

    return render(request, 'frontend/dynamic/aux_structures.html', {'structures': e.structure_set.all()})


@login_required
def calculations(request):
    profile = request.user.profile

    teammates = []
    if profile.member_of:
        for t in profile.member_of.members.all():
            teammates.append(t.username)

    if profile.member_of and profile.member_of.PI:
        teammates.append(profile.member_of.PI.username)

    if profile.researchgroup_PI:
        for g in profile.researchgroup_PI.all():
            for t in g.members.all():
                teammates.append(t.username)

    teammates = list(set(teammates))
    if profile.username in teammates:
        teammates.remove(profile.username)

    return render(request, 'frontend/calculations.html', {
            'profile': request.user.profile,
            'steps': BasicStep.objects.all(),
            'teammates': teammates,
        })

@login_required
def projects(request):
    return render(request, 'frontend/projects.html', {
            'profile': request.user.profile,
            'target_profile': request.user.profile,
            'projects': request.user.profile.project_set.all(),
        })

@login_required
def projects_username(request, username):
    target_username = clean(username)

    try:
        target_profile = User.objects.get(username=target_username).profile
    except User.DoesNotExist:
        return HttpResponse(status=404)

    if request.user.profile == target_profile:
        return render(request, 'frontend/projects.html', {
                    'profile': request.user.profile,
                    'target_profile': target_profile,
                    'projects': request.user.profile.project_set.all(),
                })
    elif profile_intersection(request.user.profile, target_profile):
        return render(request, 'frontend/projects.html', {
                    'profile': request.user.profile,
                    'target_profile': target_profile,
                    'projects': target_profile.project_set.filter(private=0),
                })

    else:
        return HttpResponse(status=404)

@login_required
def get_projects(request):
    if request.method == 'POST':
        target_username = clean(request.POST['username'])
        profile = request.user.profile

        try:
            target_profile = User.objects.get(username=target_username).profile
        except User.DoesNotExist:
            return HttpResponse(status=404)

        if profile == target_profile:
            return render(request, 'frontend/dynamic/project_list.html', {'projects' : target_profile.project_set.all()})
        elif profile_intersection(profile, target_profile):
            return render(request, 'frontend/dynamic/project_list.html', {'projects' : target_profile.project_set.filter(private=0)})
        else:
            return HttpResponse(status=404)
    else:
        return HttpResponse(status=404)

@login_required
def create_project(request):
    if request.method == 'POST':
        profile = request.user.profile
        proj = Project.objects.create(name="My Project", author=profile)
        proj.save()

        return HttpResponse("{};{}".format(proj.id, proj.name))
    else:
        return HttpResponse(status=404)

@login_required
def create_folder(request):
    if request.method == 'POST':
        profile = request.user.profile

        if "current_folder_id" not in request.POST.keys():
            return HttpResponse(status=403)

        current_folder_id = int(clean(request.POST['current_folder_id']))

        try:
            current_folder = Folder.objects.get(pk=current_folder_id)
        except Folder.DoesNotExist:
            return HttpResponse(status=404)

        if current_folder.depth > MAX_FOLDER_DEPTH:
            return HttpResponse(status=403)

        if current_folder.project.author != profile:
            return HttpResponse(status=403)

        for i in range(1, 6):
            try:
                existing_folder = Folder.objects.get(name="My Folder {}".format(i))
            except Folder.DoesNotExist:
                break
        else:
            return HttpResponse(status=403)

        folder = Folder.objects.create(name="My Folder {}".format(i), project=current_folder.project, parent_folder=current_folder)
        folder.depth = current_folder.depth + 1
        folder.save()

        return HttpResponse("{};{}".format(folder.id, folder.name))
    else:
        return HttpResponse(status=404)

@login_required
def project_details(request, username, proj):
    target_project = clean(proj)
    target_username = clean(username)

    try:
        target_profile = User.objects.get(username=target_username).profile
    except User.DoesNotExist:
        return HttpResponseRedirect("/home/")

    if profile_intersection(request.user.profile, target_profile):
        try:
            project = target_profile.project_set.get(name=target_project)
        except Project.DoesNotExist:
            return HttpResponseRedirect("/home/")
        if can_view_project(project, request.user.profile):
            molecules = []
            for m in project.molecule_set.prefetch_related('ensemble_set').all().order_by(Lower('name')):
                molecules.append(m)

            return render(request, 'frontend/project_details.html', {
            'molecules': molecules,
            'project': project,
            })
        else:
            return HttpResponseRedirect("/home/")
    else:
        return HttpResponseRedirect("/home/")

def clean(txt):
    filter(lambda x: x in string.printable, txt)
    return bleach.clean(txt)

def clean_filename(txt):
    return clean(txt).replace(' ', '_').replace('/', '_')

@login_required
def molecule(request, pk):
    try:
        mol = Molecule.objects.get(pk=pk)
    except Molecule.DoesNotExist:
        return redirect('/home/')

    if not can_view_molecule(mol, request.user.profile):
        return redirect('/home/')

    return render(request, 'frontend/molecule.html', {'profile': request.user.profile,
        'ensembles': mol.ensemble_set.filter(hidden=False),
        'molecule': mol})

@login_required
def ensemble_table_body(request, pk):
    try:
        mol = Molecule.objects.get(pk=pk)
    except Molecule.DoesNotExist:
        return redirect('/home/')

    if not can_view_molecule(mol, request.user.profile):
        return redirect('/home/')

    return render(request, 'frontend/dynamic/ensemble_table_body.html', {'profile': request.user.profile,
        'molecule': mol})

@login_required
def ensemble(request, pk):
    try:
        e = Ensemble.objects.get(pk=pk)
    except Ensemble.DoesNotExist:
        return redirect('/home/')

    if not can_view_ensemble(e, request.user.profile):
        return redirect('/home/')

    return render(request, 'frontend/ensemble.html', {'profile': request.user.profile,
        'ensemble': e})

def _get_related_calculations(e):
    orders = list(set([i.order for i in e.calculation_set.all()]))
    orders += list(e.calculationorder_set.prefetch_related('calculation_set').all())
    return orders

@login_required
def get_related_calculations(request, pk):
    try:
        e = Ensemble.objects.get(pk=pk)
    except Ensemble.DoesNotExist:
        return redirect('/home/')

    if not can_view_ensemble(e, request.user.profile):
        return redirect('/home/')

    orders = _get_related_calculations(e)
    return render(request, 'frontend/dynamic/get_related_calculations.html', {'profile': request.user.profile,
        'ensemble': e,
        'orders': orders,
        })

@login_required
def nmr_analysis(request, pk, pid):
    try:
        e = Ensemble.objects.get(pk=pk)
    except Ensemble.DoesNotExist:
        return redirect('/home/')

    if not can_view_ensemble(e, request.user.profile):
        return redirect('/home/')

    try:
        param = Parameters.objects.get(pk=pid)
    except Parameters.DoesNotExist:
        return redirect('/home/')

    if not can_view_parameters(param, request.user.profile):
        return redirect('/home/')

    return render(request, 'frontend/nmr_analysis.html', {'profile': request.user.profile,
        'ensemble': e, 'parameters': param})

def _get_shifts(request):
    if 'id' not in request.POST.keys():
        return ''

    if 'pid' not in request.POST.keys():
        return ''

    id = clean(request.POST['id'])
    pid = clean(request.POST['pid'])

    try:
        e = Ensemble.objects.get(pk=id)
    except Ensemble.DoesNotExist:
        return ''

    if not can_view_ensemble(e, request.user.profile):
        return ''

    try:
        param = Parameters.objects.get(pk=pid)
    except Parameters.DoesNotExist:
        return ''

    if not can_view_parameters(param, request.user.profile):
        return ''

    scaling_factors = {}
    if 'scaling_factors' in request.POST.keys():
        scaling_str = clean(request.POST['scaling_factors'])
        for entry in scaling_str.split(';'):
            if entry.strip() == '':
                continue

            el, m, b = entry.split(',')
            el = clean(el)
            try:
                m = float(m)
                b = float(b)
            except ValueError:
                continue
            if el not in scaling_factors.keys():
                scaling_factors[el] = [m, b]

    structures = e.structure_set.all()
    shifts = {}
    s_ids = []
    for s in structures:
        try:
            p = s.properties.get(parameters=param)
        except Property.DoesNotExist:
            continue

        if p.simple_nmr == '':
            continue

        weighted_shifts = e.weighted_nmr_shifts(param)
        for entry in weighted_shifts:
            num = int(entry[0])-1
            el = entry[1]
            shift = float(entry[2])
            shifts[num] = [el, shift, '-']

    xyz = parse_xyz_from_text(s.xyz_structure)
    eqs = equivalent_atoms(xyz)
    for nums in eqs:
        lnums = len(nums)
        eq_shift = sum([shifts[i][1] for i in nums])/lnums
        for num in nums:
            shifts[num][1] = eq_shift

    if len(scaling_factors.keys()) > 0:
        for shift in shifts.keys():
            el = shifts[shift][0]
            if el in scaling_factors.keys():
                slope, intercept = scaling_factors[el]
                s = shifts[shift][1]
                shifts[shift][2] = "{:.3f}".format((s-intercept)/slope)
    return shifts

@login_required
def get_shifts(request):

    shifts = _get_shifts(request)

    if shifts == '':
        return HttpResponse(status=404)

    CELL = """
    <tr>
            <td>{}</td>
            <td>{}</td>
            <td>{:.3f}</td>
            <td>{}</td>
    </tr>"""

    response = ""
    for shift in sorted(shifts.keys(), key=lambda l: shifts[l][2], reverse=True):
        response += CELL.format(shift, *shifts[shift])

    return HttpResponse(response)

@login_required
def get_exp_spectrum(request):
    t = time.time()
    d = "/tmp/nmr_{}".format(t)
    os.mkdir(d)
    for ind, f in enumerate(request.FILES.getlist("file")):
        in_file = f.read()#not cleaned
        with open(os.path.join(d, f.name), 'wb') as out:
            out.write(in_file)

    dic, fid = ng.fileio.bruker.read(d)

    zero_fill_size = 32768
    fid = ng.bruker.remove_digital_filter(dic, fid)
    fid = ng.proc_base.zf_size(fid, zero_fill_size) # <2>
    fid = ng.proc_base.rev(fid) # <3>
    fid = ng.proc_base.fft(fid)
    fid = ng.proc_autophase.autops(fid, 'acme')

    offset = (float(dic['acqus']['SW']) / 2.) - (float(dic['acqus']['O1']) / float(dic['acqus']['BF1']))
    start = float(dic['acqus']['SW']) - offset
    end = -offset
    step = float(dic['acqus']['SW']) / zero_fill_size

    ppms = np.arange(start, end, -step)[:zero_fill_size]

    fid = ng.proc_base.mult(fid, c=1./max(fid))
    def plotspectra(ppms, data, start=None, stop=None):
        if start: # <1>
            dx = abs(ppms - start)
            ixs = list(dx).index(min(dx))
            ppms = ppms[ixs:]
            data = data[:,ixs:]
        if stop:
            dx = abs(ppms - stop)
            ixs = list(dx).index(min(dx))
            ppms = ppms[:ixs]
            data = data[:,:ixs]

        return ppms, data

    ppms, fid = plotspectra(ppms, np.array([fid]), start=10, stop=0)
    shifts = _get_shifts(request)
    if shifts == '':
        response = "PPM,Signal\n"
        for x, y in zip(ppms[0::10], fid[0,0::10]):
            response += "{},{}\n".format(-x, np.real(y))

        return HttpResponse(response)
    else:
        sigma = 0.001
        def plot_peaks(_x, PP):
            val = 0
            for w in PP:
                dd = list(abs(ppms - w))
                T = fid[0,dd.index(min(dd))]
                val += np.exp(-(_x-w)**2/sigma)
            val = val/max(val)
            return val
        _ppms = ppms[0::10]
        _fid = fid[0,0::10]/max(fid[0,0::10])
        l_shifts = [float(shifts[i][2]) for i in shifts if shifts[i][0] == 'H']
        pred = plot_peaks(_ppms, l_shifts)
        response = "PPM,Signal,Prediction\n"
        for x, y, z in zip(_ppms, _fid, pred):
            response += "{:.3f},{:.3f},{:.3f}\n".format(-x, np.real(y), z)
        return HttpResponse(response)

@login_required
def link_order(request, pk):
    try:
        o = CalculationOrder.objects.get(pk=pk)
    except CalculationOrder.DoesNotExist:
        return HttpResponseRedirect("/calculations/")

    profile = request.user.profile

    if not can_view_order(o, profile) or o.calculation_set.count() == 0:
        return HttpResponseRedirect("/calculations/")

    if profile == o.author:
        if o.new_status:
            o.last_seen_status = o.status
            o.author.unseen_calculations = max(o.author.unseen_calculations-1, 0)
            o.author.save()
            o.save()

    if o.result_ensemble:
        return HttpResponseRedirect("/ensemble/{}".format(o.result_ensemble.id))
    else:
        if o.ensemble is not None:
            return HttpResponseRedirect("/ensemble/{}".format(o.ensemble.id))
        elif o.structure:
            return HttpResponseRedirect("/ensemble/{}".format(o.structure.parent_ensemble.id))
        else:
            return HttpResponseRedirect("/calculations/")

@login_required
def details_ensemble(request):
    if request.method == 'POST':
        pk = int(clean(request.POST['id']))
        try:
            p_id = int(clean(request.POST['p_id']))
        except KeyError:
            return HttpResponse(status=204)

        try:
            e = Ensemble.objects.get(pk=pk)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)
        try:
            p = Parameters.objects.get(pk=p_id)
        except Parameters.DoesNotExist:
            return HttpResponse(status=403)

        if not can_view_ensemble(e, request.user.profile):
            return HttpResponse(status=403)

        if e.has_nmr(p):
            shifts = e.weighted_nmr_shifts(p)
            return render(request, 'frontend/dynamic/details_ensemble.html', {'profile': request.user.profile,
                'ensemble': e, 'parameters': p, 'shifts': shifts})
        else:
            return render(request, 'frontend/dynamic/details_ensemble.html', {'profile': request.user.profile,
                'ensemble': e, 'parameters': p})

    return HttpResponse(status=403)

@login_required
def details_structure(request):
    if request.method == 'POST':
        pk = int(clean(request.POST['id']))
        try:
            p_id = int(clean(request.POST['p_id']))
            num = int(clean(request.POST['num']))
        except KeyError:
            return HttpResponse(status=204)

        try:
            e = Ensemble.objects.get(pk=pk)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)

        if not can_view_ensemble(e, request.user.profile):
            return HttpResponse(status=403)

        try:
            s = e.structure_set.get(number=num)
        except Structure.DoesNotExist:
            return HttpResponse(status=403)

        try:
            p = Parameters.objects.get(pk=p_id)
        except Parameters.DoesNotExist:
            return HttpResponse(status=403)

        for prop in s.properties.all():
            if prop.parameters == p:
                break
        else:
            return HttpResponse(status=404)

        return render(request, 'frontend/dynamic/details_structure.html', {'profile': request.user.profile,
            'structure': s, 'property': prop, 'ensemble': e})

    return HttpResponse(status=403)

def learn(request):
    examples = Example.objects.all()
    recipes = Recipe.objects.all()

    return render(request, 'frontend/learn.html', {'examples': examples, 'recipes': recipes})

def example(request, pk):
    try:
        ex = Example.objects.get(pk=pk)
    except Example.DoesNotExist:
        pass

    return render(request, 'examples/' + ex.page_path, {})

def recipe(request, pk):
    try:
        r = Recipe.objects.get(pk=pk)
    except Recipe.DoesNotExist:
        pass

    return render(request, 'recipes/' + r.page_path, {})

class RegisterView(generic.CreateView):
    form_class = UserCreateForm
    template_name = 'registration/signup.html'
    model = Profile
    success_url = '/accounts/login/'

def please_register(request):
        return render(request, 'frontend/please_register.html', {})

def error(request, msg):
    return render(request, 'frontend/error.html', {
        'profile': request.user.profile,
        'error_message': msg,
        })

def parse_parameters(request):
    profile = request.user.profile

    if 'calc_type' in request.POST.keys():
        try:
            step = BasicStep.objects.get(name=clean(request.POST['calc_type']))
        except BasicStep.DoesNotExist:
            return "No such procedure"
    else:
        return "No calculation type"

    if 'calc_project' in request.POST.keys():
        project = clean(request.POST['calc_project'])
        if project.strip() == '':
            return "No calculation project"
    else:
        return "No calculation project"

    if 'calc_charge' in request.POST.keys():
        try:
            charge = int(clean(request.POST['calc_charge']).replace('+', ''))
        except ValueError:
            return "Invalid calculation charge"
    else:
        return "No calculation charge"

    if 'calc_multiplicity' in request.POST.keys():
        try:
            mult = int(clean(request.POST['calc_multiplicity']))
        except ValueError:
            return "Invalid multiplicity"
        if mult < 1:
            return "Invalid multiplicity"
    else:
        return "No calculation multiplicity"

    if 'calc_solvent' in request.POST.keys():
        solvent = clean(request.POST['calc_solvent'])
        if solvent.strip() == '':
            solvent = "Vacuum"
    else:
        solvent = "Vacuum"

    if solvent != "Vacuum":
        if 'calc_solvation_model' in request.POST.keys():
            solvation_model = clean(request.POST['calc_solvation_model'])
            if solvation_model not in ['SMD', 'PCM', 'CPCM', 'GBSA', 'ALPB']:
                return "Invalid solvation model"
            if 'calc_solvation_radii' in request.POST.keys():
                solvation_radii = clean(request.POST['calc_solvation_radii'])
            else:
                return "No solvation radii"
        else:
            return "No solvation model"
    else:
        solvation_model = ""
        solvation_radii = ""

    if 'calc_software' in request.POST.keys():
        software = clean(request.POST['calc_software'])
        if software.strip() == '':
            return "No software chosen"
        if software not in BASICSTEP_TABLE.keys():
            return "Invalid software chosen"
    else:
        return "No software chosen"

    if 'calc_df' in request.POST.keys():
        df = clean(request.POST['calc_df'])
    else:
        df = ''

    if 'calc_custom_bs' in request.POST.keys():
        bs = clean(request.POST['calc_custom_bs'])
    else:
        bs = ''

    if software == 'ORCA' or software == 'Gaussian':
        if 'calc_theory_level' in request.POST.keys():
            theory = clean(request.POST['calc_theory_level'])
            if theory.strip() == '':
                return "No theory level chosen"
        else:
            return "No theory level chosen"

        if theory == "DFT":
            special_functional = False
            if 'pbeh3c' in request.POST.keys() and software == "ORCA":
                field_pbeh3c = clean(request.POST['pbeh3c'])
                if field_pbeh3c == "on":
                    special_functional = True
                    functional = "PBEh-3c"
                    basis_set = ""

            if not special_functional:
                if 'calc_functional' in request.POST.keys():
                    functional = clean(request.POST['calc_functional'])
                    if functional.strip() == '':
                        return "No method"
                else:
                    return "No method"
                if functional not in SPECIAL_FUNCTIONALS:
                    if 'calc_basis_set' in request.POST.keys():
                        basis_set = clean(request.POST['calc_basis_set'])
                        if basis_set.strip() == '':
                            return "No basis set chosen"
                    else:
                        return "No basis set chosen"
                else:
                    basis_set = ""
        elif theory == "Semi-empirical":
            if 'calc_se_method' in request.POST.keys():
                functional = clean(request.POST['calc_se_method'])
                if functional.strip() == '':
                    return "No semi-empirical method chosen"
                basis_set = ''
            else:
                return "No semi-empirical method chosen"
        elif theory == "HF":
            special_functional = False
            if 'hf3c' in request.POST.keys() and software == "ORCA":
                field_hf3c = clean(request.POST['hf3c'])
                if field_hf3c == "on":
                    special_functional = True
                    functional = "HF-3c"
                    basis_set = ""

            if not special_functional:
                functional = "HF"
                if 'calc_basis_set' in request.POST.keys():
                    basis_set = clean(request.POST['calc_basis_set'])
                    if basis_set.strip() == '':
                        return "No basis set chosen"
                else:
                    return "No basis set chosen"
        elif theory == "RI-MP2":
            if software != "ORCA":
                return "RI-MP2 is only available for ORCA"

            functional = "RI-MP2"
            if 'calc_basis_set' in request.POST.keys():
                basis_set = clean(request.POST['calc_basis_set'])
                if basis_set.strip() == '':
                    return "No basis set chosen"
            else:
                return "No basis set chosen"
        else:
            return "Invalid theory level"

    else:
        theory = "GFN2-xTB"
        if software == "xtb":
            functional = "GFN2-xTB"
            basis_set = "min"
            if step.name == "Conformational Search":
                if 'calc_conf_option' in request.POST.keys():
                    conf_option = clean(request.POST['calc_conf_option'])
                    if conf_option not in ['GFN2-xTB', 'GFN-FF', 'GFN2-xTB//GFN-FF']:
                        return "Error in the option for the conformational search"
                    functional = conf_option
        else:
            functional = ""
            basis_set = ""

    if len(project) > 100:
        return "The chosen project name is too long"

    if step.name not in BASICSTEP_TABLE[software].keys():
        return "Invalid calculation type"

    if 'calc_specifications' in request.POST.keys():
        specifications = clean(request.POST['calc_specifications']).lower()
    else:
        specifications = ""

    if project == "New Project":
        new_project_name = clean(request.POST['new_project_name'])
        try:
            project_obj = Project.objects.get(name=new_project_name, author=profile)
        except Project.DoesNotExist:
            project_obj = Project.objects.create(name=new_project_name, author=profile)
            project_obj.save()
        else:
            logger.info("Project with that name already exists")
    else:
        try:
            project_set = profile.project_set.filter(name=project)
        except Profile.DoesNotExist:
            return "No such project"

        if len(project_set) != 1:
            return "More than one project with the same name found!"
        else:
            project_obj = project_set[0]

    params = Parameters.objects.create(charge=charge, multiplicity=mult, solvent=solvent, method=functional, basis_set=basis_set, software=software, theory_level=theory, solvation_model=solvation_model, solvation_radii=solvation_radii, density_fitting=df, custom_basis_sets=bs, specifications=specifications)
    params.save()

    return params, project_obj, step

@login_required
def save_preset(request):
    ret = parse_parameters(request)

    if isinstance(ret, str):
        return HttpResponse(ret)
    params, project_obj, step = ret

    if 'preset_name' in request.POST.keys():
        preset_name = clean(request.POST['preset_name'])
    else:
        return HttpResponse("No preset name")

    preset = Preset.objects.create(name=preset_name, author=request.user.profile, params=params)
    preset.save()
    return HttpResponse("Preset created")

@login_required
def set_project_default(request):
    ret = parse_parameters(request)

    if isinstance(ret, str):
        return HttpResponse(ret)

    params, project_obj, step = ret

    preset = Preset.objects.create(name="{} Default".format(project_obj.name), author=request.user.profile, params=params)
    preset.save()

    if project_obj.preset is not None:
        project_obj.preset.delete()

    project_obj.preset = preset
    project_obj.save()

    return HttpResponse("Default parameters updated")

def handle_file_upload(ff, params):
    s = Structure.objects.create()

    _params = Parameters.objects.create(software="Unknown", method="Unknown", basis_set="", solvation_model="", charge=params.charge, multiplicity=1)
    p = Property.objects.create(parent_structure=s, parameters=_params, geom=True)
    p.save()
    _params.save()

    drawing = False
    in_file = clean(ff.read().decode('utf-8'))
    fname = clean(ff.name)
    filename = '.'.join(fname.split('.')[:-1])
    ext = fname.split('.')[-1]

    if ext == 'mol':
        s.mol_structure = in_file
        generate_xyz_structure(False, s)
    elif ext == 'xyz':
        s.xyz_structure = in_file
    elif ext == 'sdf':
        s.sdf_structure = in_file
        generate_xyz_structure(False, s)
    elif ext == 'mol2':
        s.mol2_structure = in_file
        generate_xyz_structure(False, s)
    elif ext == 'log':
        s.xyz_structure = get_Gaussian_xyz(in_file)
    elif ext in ['com', 'gjf']:
        s.xyz_structure = get_xyz_from_Gaussian_input(in_file)
    else:
        "Unknown file extension (Known formats: .mol, .mol2, .xyz, .sdf, .com, .gjf)"
    s.save()
    return s, filename

def process_filename(filename):
    if filename.find("_conf") != -1:
        sname = filename.split("_conf")
        if len(sname) > 2:
            return filename, 0
        filename = sname[0]
        try:
            num = int(sname[1])
        except ValueError:
            return filename, 0
        else:
            return filename, num
    else:
        return filename, 0

@login_required
def submit_calculation(request):
    ret = parse_parameters(request)

    if isinstance(ret, str):
        return error(request, ret)

    profile = request.user.profile

    params, project_obj, step = ret

    if 'calc_resource' in request.POST.keys():
        resource = clean(request.POST['calc_resource'])
        if resource.strip() == '':
            return error(request, "No computing resource chosen")
    else:
        return error(request, "No computing resource chosen")

    if resource != "Local":
        try:
            access = ClusterAccess.objects.get(cluster_address=resource, owner=profile)
        except ClusterAccess.DoesNotExist:
            return error(request, "No such cluster access")

        if access.owner != profile:
            return error(request, "You do not have the right to use this cluster access")
    else:
        if not profile.is_PI and profile.group == None and not request.user.is_superuser:
            return error(request, "You have no computing resource")

    orders = []
    drawing = True

    def get_default_name():
        return step.name + " Result"

    if 'calc_name' in request.POST.keys():
        name = clean(request.POST['calc_name'])

        if name.strip() == '' and step.creates_ensemble:
            name = get_default_name()
    else:
        name = get_default_name()

    if 'calc_mol_name' in request.POST.keys():
        mol_name = clean(request.POST['calc_mol_name'])
    else:
        mol_name = ''

    if len(name) > 100:
        return error(request, "The chosen ensemble name is too long")

    if len(mol_name) > 100:
        return error(request, "The chosen molecule name is too long")

    if 'starting_ensemble' in request.POST.keys():
        start_id = int(clean(request.POST['starting_ensemble']))
        try:
            start_e = Ensemble.objects.get(pk=start_id)
        except Ensemble.DoesNotExist:
            return error(request, "No starting ensemble found")

        start_author = start_e.parent_molecule.project.author
        if not can_view_ensemble(start_e, profile):
            return error(request, "You do not have permission to access the starting calculation")

        if step.creates_ensemble:
            order_name = name
        else:
            order_name = start_e.name

        filter = None
        if 'starting_structs' in request.POST.keys():
            structs_str = clean(request.POST['starting_structs'])
            structs_nums = [int(i) for i in structs_str.split(',')]

            avail_nums = [i['number'] for i in start_e.structure_set.values('number')]

            for s_num in structs_nums:
                if s_num not in avail_nums:
                    return error(request, "Invalid starting structures")

            filter = Filter.objects.create(type="By Number", value=structs_str)
        elif 'calc_filter' in request.POST.keys():
            filter_type = clean(request.POST['calc_filter'])
            if filter_type == "None":
                pass
            elif filter_type == "By Relative Energy" or filter_type == "By Boltzmann Weight":
                if 'filter_value' in request.POST.keys():
                    try:
                        filter_value = float(clean(request.POST['filter_value']))
                    except ValueError:
                        return error(request, "Invalid filter value")
                else:
                    return error(request, "No filter value")

                if 'filter_parameters' in request.POST.keys():
                    try:
                        filter_parameters_id = int(clean(request.POST['filter_parameters']))
                    except ValueError:
                        return error(request, "Invalid filter parameters")

                    try:
                        filter_parameters = Parameters.objects.get(pk=filter_parameters_id)
                    except Parameters.DoesNotExist:
                        return error(request, "Invalid filter parameters")

                    if not can_view_parameters(filter_parameters, profile):
                        return error(request, "Invalid filter parameters")

                    filter = Filter.objects.create(type=filter_type, parameters=filter_parameters, value=filter_value)
                else:
                    return error(request, "No filter parameters")

            else:
                return error(request, "Invalid filter type")

        obj = CalculationOrder.objects.create(name=order_name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, ensemble=start_e)

        if filter is not None:
            obj.filter = filter

        orders.append(obj)
    elif 'starting_calc' in request.POST.keys():
        if not 'starting_frame' in request.POST.keys():
            return error(request, "Missing starting frame number")

        c_id = int(clean(request.POST['starting_calc']))
        frame_num = int(clean(request.POST['starting_frame']))

        try:
            start_c = Calculation.objects.get(pk=c_id)
        except Calculation.DoesNotExist:
            return error(request, "No starting ensemble found")
        if not can_view_calculation(start_c, profile):
            return error(request, "You do not have permission to access the starting calculation")

        if step.creates_ensemble:
            order_name = name
        else:
            order_name = start_c.result_ensemble.name + " - Frame {}".format(frame_num)

        obj = CalculationOrder.objects.create(name=order_name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, start_calc=start_c, start_calc_frame=frame_num)
        orders.append(obj)
    else:
        if mol_name == '':
            return error(request, "Missing molecule name")

        if len(request.FILES) > 0:
            combine = ""
            if 'calc_combine_files' in request.POST.keys():
                combine = clean(request.POST['calc_combine_files'])

            parse_filenames = ""
            if 'calc_parse_filenames' in request.POST.keys():
                parse_filenames = clean(request.POST['calc_parse_filenames'])

            files = request.FILES.getlist("file_structure")
            if len(files) > 1:
                if combine == "on" and parse_filenames != "on":
                    mol = Molecule.objects.create(name=mol_name, project=project_obj)
                    e = Ensemble.objects.create(name="File Upload", parent_molecule=mol)
                    for ind, ff in enumerate(files):
                        ss = handle_file_upload(ff, params)
                        if isinstance(ss, str):
                            e.structure_set.all().delete()
                            e.delete()
                            mol.delete()
                            return error(request, ss)
                        struct, filename = ss

                        if ind == 0:
                            fing = gen_fingerprint(struct)
                            mol.inchi = fing
                            mol.save()

                        struct.number = ind+1
                        struct.parent_ensemble = e
                        struct.save()

                    obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, ensemble=e)
                    orders.append(obj)
                elif combine != "on" and parse_filenames == "on":
                    unique_molecules = {}
                    for ff in files:
                        ss = handle_file_upload(ff, params)
                        if isinstance(ss, str):
                            for _mol_name, arr_structs in unique_molecules.items():
                                for struct in arr_structs:
                                    struct.delete()
                            return error(request, ss)
                        struct, filename = ss

                        _mol_name, num = process_filename(filename)

                        struct.number = num
                        struct.save()
                        if _mol_name in unique_molecules.keys():
                            unique_molecules[_mol_name].append(struct)
                        else:
                            unique_molecules[_mol_name] = [struct]

                    for _mol_name, arr_structs in unique_molecules.items():
                        used_numbers = []
                        fing = gen_fingerprint(arr_structs[0])
                        mol = Molecule.objects.create(name=_mol_name, inchi=fing, project=project_obj)
                        mol.save()

                        e = Ensemble.objects.create(name="File Upload", parent_molecule=mol)
                        for struct in arr_structs:
                            if struct.number == 0:
                                num = 1
                                while num in used_numbers:
                                    num += 1
                                struct.number = num
                                used_numbers.append(num)
                            else:
                                used_numbers.append(struct.number)

                            struct.parent_ensemble = e
                            struct.save()
                        obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, ensemble=e)

                        orders.append(obj)
                elif combine == "on" and parse_filenames == "on":
                    ss = handle_file_upload(files[0], params)
                    if isinstance(ss, HttpResponse):
                        return ss

                    struct, filename = ss
                    _mol_name, num = process_filename(filename)
                    fing = gen_fingerprint(struct)

                    mol = Molecule.objects.create(name=_mol_name, project=project_obj, inchi=fing)
                    e = Ensemble.objects.create(name="File Upload", parent_molecule=mol)
                    struct.number = 1
                    struct.parent_ensemble = e
                    struct.save()

                    for ind, ff in enumerate(files[1:]):
                        ss = handle_file_upload(ff, params)
                        if isinstance(ss, HttpResponse):
                            e.structure_set.all().delete()
                            e.delete()
                            mol.delete()
                            return ss
                        struct, filename = ss
                        struct.number = ind+2
                        struct.parent_ensemble = e
                        struct.save()
                    obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, ensemble=e)
                    orders.append(obj)
                else:
                    unique_molecules = {}
                    for ff in files:
                        ss = handle_file_upload(ff, params)
                        if isinstance(ss, HttpResponse):
                            for _mol_name, arr_structs in unique_molecules.items():
                                for struct in arr_structs:
                                    struct.delete()
                            return ss
                        struct, filename = ss

                        fing = gen_fingerprint(struct)
                        if fing in unique_molecules.keys():
                            unique_molecules[fing].append(struct)
                        else:
                            unique_molecules[fing] = [struct]

                    for ind, (fing, arr_struct) in enumerate(unique_molecules.items()):
                        if len(unique_molecules.keys()) > 1:
                            mol = Molecule.objects.create(name="{} set {}".format(mol_name, ind+1), inchi=fing, project=project_obj)
                        else:
                            mol = Molecule.objects.create(name=mol_name, inchi=fing, project=project_obj)
                        e = Ensemble.objects.create(name="File Upload", parent_molecule=mol)

                        for s_num, struct in enumerate(arr_struct):
                            struct.parent_ensemble = e
                            struct.number = s_num + 1
                            struct.save()

                        obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj, ensemble=e)
                        orders.append(obj)
            elif len(files) == 1:
                ff = files[0]
                ss = handle_file_upload(ff, params)
                if isinstance(ss, HttpResponse):
                    return ss
                struct, filename = ss

                num = 1
                if parse_filenames == "on":
                    _mol_name, num = process_filename(names[struct.id]) # Disable mol_name
                else:
                    _mol_name = mol_name

                obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj)

                fing = gen_fingerprint(struct)
                mol = Molecule.objects.create(name=_mol_name, inchi=fing, project=project_obj)
                mol.save()

                e = Ensemble.objects.create(name="File Upload", parent_molecule=mol)
                struct.parent_ensemble = e
                struct.number = num
                struct.save()

                obj.ensemble = e
                obj.save()
                orders.append(obj)
        else:# No file upload
            if 'structure' in request.POST.keys():
                obj = CalculationOrder.objects.create(name=name, date=timezone.now(), parameters=params, author=profile, step=step, project=project_obj)
                drawing = True
                mol_obj = Molecule.objects.create(name=mol_name, project=project_obj)
                e = Ensemble.objects.create(name="Drawn Structure", parent_molecule=mol_obj)
                obj.ensemble = e

                s = Structure.objects.create(parent_ensemble=e, number=1)
                params = Parameters.objects.create(software="Open Babel", method="Forcefield", basis_set="", solvation_model="", charge=params.charge, multiplicity="1")
                p = Property.objects.create(parent_structure=s, parameters=params, geom=True)
                p.save()
                params.save()

                mol = clean(request.POST['structure'])
                s.mol_structure = mol
                s.save()
                orders.append(obj)
            else:
                return error(request, "No input structure")

    if step.name == "Minimum Energy Path":
        if len(orders) != 1:
            return error(request, 'Only one initial structure can be used')

        if len(request.FILES.getlist("aux_file_structure")) == 1:
            _aux_struct = handle_file_upload(request.FILES.getlist("aux_file_structure")[0], params)
            if isinstance(_aux_struct, HttpResponse):
                return _aux_struct
            aux_struct = _aux_struct[0]
        else:
            if 'aux_struct' not in request.POST.keys():
                return error(request, 'No valid auxiliary structure')
            try:
                aux_struct = Structure.objects.get(pk=int(clean(request.POST['aux_struct'])))
            except Structure.DoesNotExist:
                return error(request, 'No valid auxiliary structure')
            if not can_view_structure(aux_struct, profile):
                return error(request, 'No valid auxiliary structure')

        aux_struct.save()
        orders[0].aux_structure = aux_struct

    TYPE_LENGTH = {'Distance' : 2, 'Angle' : 3, 'Dihedral' : 4}
    constraints = ""
    if step.name in ["Constrained Optimisation", "Constrained Conformational Search"] and 'constraint_num' in request.POST.keys():
        for ind in range(1, int(request.POST['constraint_num'])+1):
            try:
                mode = clean(request.POST['constraint_mode_{}'.format(ind)])
            except MultiValueDictKeyError:
                pass
            else:
                _type = clean(request.POST['constraint_type_{}'.format(ind)])
                ids = []
                for i in range(1, TYPE_LENGTH[_type]+1):
                    id_txt = clean(request.POST['calc_constraint_{}_{}'.format(ind, i)])
                    if id_txt != "":
                        id = int(id_txt)
                        ids.append(id)

                if len(ids) == 0:
                    return error(request, "Invalid or missing constraints")

                ids = '_'.join([str(i) for i in ids])
                if mode == "Freeze":
                    constraints += "{}/{};".format(mode, ids)
                elif mode == "Scan":
                    obj.has_scan = True

                    if params.software != "Gaussian":
                        try:
                            begin = float(clean(request.POST['calc_scan_{}_1'.format(ind)]))
                        except ValueError:
                            return error(request, "Invalid scan parameters")
                    else:
                        begin = 42.0
                    try:
                        end = float(clean(request.POST['calc_scan_{}_2'.format(ind)]))
                        steps = int(clean(request.POST['calc_scan_{}_3'.format(ind)]))
                    except ValueError:
                        return error(request, "Invalid scan parameters")
                    constraints += "{}_{}_{}_{}/{};".format(mode, begin, end, steps, ids)
                else:
                    return error(request, "Invalid constrained optimisation")

        if constraints == "":
            return error(request, "No constraint specified for constrained calculation")

    obj.save()
    for o in orders:
        o.constraints = constraints

        if resource != "Local":
            o.resource = access

        o.save()

    profile.save()

    if 'test' not in request.POST.keys():
        for o in orders:
            dispatcher.delay(drawing, o.id)

    return redirect("/calculations/")

def can_view_project(proj, profile):
    if proj.author == profile:
        return True
    else:
        if not profile_intersection(proj.author, profile):
            return False
        if proj.private and not profile.is_PI:
            return False
        return True

def can_view_molecule(mol, profile):
    return can_view_project(mol.project, profile)

def can_view_ensemble(e, profile):
    return can_view_molecule(e.parent_molecule, profile)

def can_view_structure(s, profile):
    return can_view_ensemble(s.parent_ensemble, profile)

def can_view_parameters(p, profile):
    prop = p.property_set.first()

    if prop is not None:
        return can_view_structure(prop.parent_structure, profile)

    c = p.calculation_set.first()

    if c is not None:
        return can_view_calculation(c, profile)

def can_view_preset(p, profile):
    return profile_intersection(p.author, profile)

def can_view_order(order, profile):
    if order.author == profile:
        return True
    elif profile_intersection(order.author, profile):
        if order.project.private and not profile.is_PI:
            return False
        return True

def can_view_calculation(calc, profile):
    return can_view_order(calc.order, profile)

def profile_intersection(profile1, profile2):
    if profile1 == profile2:
        return True
    if profile1.group != None:
        if profile2 in profile1.group.members.all():
            return True
        if profile2 == profile1.group.PI:
            return True
    else:
        return False

    if profile2.group == None:
        return False

    if profile1.researchgroup_PI != None:
        for group in profile1.researchgroup_PI:
            if profile2 in group.members.all():
                return True
    return False

@login_required
def project_list(request):
    if request.method == "POST":
        target_username = clean(request.POST['user'])
        try:
            target_profile = User.objects.get(username=target_username).profile
        except User.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if not profile_intersection(profile, target_profile):
            return HttpResponse(status=403)

        return render(request, 'frontend/dynamic/project_list.html', {
                'profile': request.user.profile,
                'target_profile': target_profile,
            })

    else:
        return HttpResponse(status=403)

@login_required
def delete_project(request):
    if request.method == 'POST':
        if 'id' in request.POST.keys():
            proj_id = int(clean(request.POST['id']))
        else:
            return HttpResponse(status=403)

        try:
            to_delete = Project.objects.get(pk=proj_id)
        except Project.DoesNotExist:
            return HttpResponse(status=403)

        if to_delete.author != request.user.profile:
            return HttpResponse(status=403)

        del_project.delay(proj_id)
        return HttpResponse(status=204)
    else:
        return HttpResponse(status=403)

@login_required
def delete_order(request):
    if request.method == 'POST':
        if 'id' in request.POST.keys():
            order_id = int(clean(request.POST['id']))
        else:
            return HttpResponse(status=403)

        try:
            to_delete = CalculationOrder.objects.get(pk=order_id)
        except Project.DoesNotExist:
            return HttpResponse(status=403)

        if to_delete.author != request.user.profile:
            return HttpResponse(status=403)

        del_order.delay(order_id)
        return HttpResponse(status=204)
    else:
        return HttpResponse(status=403)

@login_required
def delete_folder(request):
    if request.method == 'POST':
        if 'id' in request.POST.keys():
            folder_id = int(clean(request.POST['id']))
        else:
            return HttpResponse(status=403)

        try:
            to_delete = Folder.objects.get(pk=folder_id)
        except Project.DoesNotExist:
            return HttpResponse(status=403)

        if to_delete.project.author != request.user.profile:
            return HttpResponse(status=403)

        if to_delete.ensemble_set.count() == 0 and to_delete.folder_set.count() == 0:## To modify?
            to_delete.delete()
            return HttpResponse(status=204)

        return HttpResponse(status=400)
    else:
        return HttpResponse(status=403)

@login_required
def delete_molecule(request):
    if request.method == 'POST':
        if 'id' in request.POST.keys():
            mol_id = int(clean(request.POST['id']))
        else:
            return HttpResponse(status=403)

        try:
            to_delete = Molecule.objects.get(pk=mol_id)
        except Molecule.DoesNotExist:
            return HttpResponse(status=403)

        if to_delete.project.author != request.user.profile:
            return HttpResponse(status=403)

        del_molecule.delay(mol_id)
        return HttpResponse(status=204)
    else:
        return HttpResponse(status=403)

@login_required
def delete_ensemble(request):
    if request.method == 'POST':
        if 'id' in request.POST.keys():
            ensemble_id = int(clean(request.POST['id']))
        else:
            return HttpResponse(status=403)

        try:
            to_delete = Ensemble.objects.get(pk=ensemble_id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=404)

        if to_delete.parent_molecule.project.author != request.user.profile:
            return HttpResponse(status=403)

        del_ensemble.delay(ensemble_id)
        return HttpResponse(status=204)
    else:
        return HttpResponse(status=403)

@login_required
def add_clusteraccess(request):
    if request.method == 'POST':
        address = clean(request.POST['cluster_address'])
        username = clean(request.POST['cluster_username'])
        pal = int(clean(request.POST['cluster_cores']))
        memory = int(clean(request.POST['cluster_memory']))
        password = clean(request.POST['cluster_password'])

        owner = request.user.profile

        try:
            existing_access = owner.clusteraccess_owner.get(cluster_address=address, cluster_username=username, owner=owner)
        except ClusterAccess.DoesNotExist:
            pass
        else:
            return HttpResponse(status=403)

        access = ClusterAccess.objects.create(cluster_address=address, cluster_username=username, owner=owner, pal=pal, memory=memory)
        access.save()
        owner.save()

        key = rsa.generate_private_key(backend=default_backend(), public_exponent=65537, key_size=2048)

        public_key = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)

        pem = key.private_bytes(encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.TraditionalOpenSSL, encryption_algorithm=serialization.BestAvailableEncryption(bytes(password, 'UTF-8')))
        with open(os.path.join(CALCUS_KEY_HOME, str(access.id)), 'wb') as out:
            out.write(pem)

        with open(os.path.join(CALCUS_KEY_HOME, str(access.id) + '.pub'), 'wb') as out:
            out.write(public_key)
            out.write(b' %b@CalcUS' % bytes(owner.username, 'utf-8'))

        return HttpResponse(public_key.decode('utf-8'))
    else:
        return HttpResponse(status=403)

@login_required
def connect_access(request):
    pk = clean(request.POST['access_id'])
    password = clean(request.POST['password'])

    try:
        access = ClusterAccess.objects.get(pk=pk)
    except ClusterAccess.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    access.status = "Pending"
    access.save()

    send_cluster_command("connect\n{}\n{}\n".format(pk, password))

    return HttpResponse("")

@login_required
def disconnect_access(request):
    pk = clean(request.POST['access_id'])

    access = ClusterAccess.objects.get(pk=pk)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    send_cluster_command("disconnect\n{}\n".format(pk))

    return HttpResponse("")

@login_required
def status_access(request):
    pk = clean(request.POST['access_id'])

    access = ClusterAccess.objects.get(pk=pk)

    profile = request.user.profile

    if access.owner != profile and not profile.user.is_superuser:
        return HttpResponse(status=403)

    dt = timezone.now() - access.last_connected
    if dt.total_seconds() < 600:
        return HttpResponse("Connected as of {} seconds ago".format(int(dt.total_seconds())))
    else:
        return HttpResponse("Disconnected")

@login_required
def get_command_status(request):
    pk = request.POST['access_id']

    try:
        access = ClusterAccess.objects.get(pk=pk)
    except ClusterAccess.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    return HttpResponse(access.status)

@login_required
def delete_access(request, pk):
    try:
        access = ClusterAccess.objects.get(pk=pk)
    except ClusterAccess.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    access.delete()

    send_cluster_command("delete_access\n{}\n".format(pk))

    return HttpResponseRedirect("/profile")

@login_required
def load_pub_key(request, pk):
    try:
        access = ClusterAccess.objects.get(pk=pk)
    except ClusterAccess.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    key_path = os.path.join(CALCUS_KEY_HOME, '{}.pub'.format(access.id))

    if not os.path.isfile(key_path):
        return HttpResponse(status=404)

    with open(key_path) as f:
        pub_key = f.readlines()[0]

    return HttpResponse(pub_key)

@login_required
def update_access(request):
    vals = {}
    for param in ['access_id', 'pal', 'mem']:
        if param not in request.POST.keys():
            return HttpResponse(status=400)

        try:
            vals[param] = int(clean(request.POST[param]))
        except ValueError:
            return HttpResponse("Invalid value", status=400)

    try:
        access = ClusterAccess.objects.get(pk=vals['access_id'])
    except ClusterAccess.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    if vals['pal'] < 1:
        return HttpResponse("Invalid number of cores", status=400)

    if vals['mem'] < 1:
        return HttpResponse("Invalid amount of memory", status=400)

    msg = ""
    curr_mem = access.memory
    curr_pal = access.pal

    if vals['pal'] != curr_pal:
        access.pal = vals['pal']
        msg += "Number of cores set to {}\n".format(vals['pal'])

    if vals['mem'] != curr_mem:
        access.memory = vals['mem']
        msg += "Amount of memory set to {} MB\n".format(vals['mem'])

    access.save()
    if msg == "":
        msg = "No change detected"

    return HttpResponse(msg)

@login_required
@superuser_required
def get_pi_requests(request):
    reqs = PIRequest.objects.count()
    return HttpResponse(str(reqs))

@login_required
@superuser_required
def get_pi_requests_table(request):

    reqs = PIRequest.objects.all()

    return render(request, 'frontend/dynamic/pi_requests_table.html', {
        'profile': request.user.profile,
        'reqs': reqs,
        })

@login_required
@superuser_required
def server_summary(request):
    users = Profile.objects.all()
    groups = ResearchGroup.objects.all()
    accesses = ClusterAccess.objects.all()
    return render(request, 'frontend/server_summary.html', {
        'users': users,
        'groups': groups,
        'accesses': accesses,
        })

@login_required
def add_user(request):
    if request.method == "POST":
        profile = request.user.profile

        if not profile.is_PI:
            return HttpResponse(status=403)

        username = clean(request.POST['username'])
        group_id = int(clean(request.POST['group_id']))

        try:
            group = ResearchGroup.objects.get(pk=group_id)
        except ResearchGroup.DoesNotExist:
            return HttpResponse(status=403)

        if group.PI != profile:
            return HttpResponse(status=403)

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return HttpResponse(status=403)

        if user.profile == profile:
            return HttpResponse(status=403)

        code = clean(request.POST['code'])

        if user.profile.code != code:
            return HttpResponse(status=403)

        group.members.add(user.profile)

        return HttpResponse(status=200)

    return HttpResponse(status=403)

@login_required
def remove_user(request):
    if request.method == "POST":
        profile = request.user.profile

        if not profile.is_PI:
            return HttpResponse(status=403)

        member_id = int(clean(request.POST['member_id']))
        group_id = int(clean(request.POST['group_id']))

        try:
            group = ResearchGroup.objects.get(pk=group_id)
        except ResearchGroup.DoesNotExist:
            return HttpResponse(status=403)

        if group.PI != profile:
            return HttpResponse(status=403)

        try:
            member = Profile.objects.get(pk=member_id)
        except Profile.DoesNotExist:
            return HttpResponse(status=403)

        if member == profile:
            return HttpResponse(status=403)

        if member not in group.members.all():
            return HttpResponse(status=403)

        group.members.remove(member)

        return HttpResponse(status=200)

    return HttpResponse(status=403)

@login_required
def profile_groups(request):
    return render(request, 'frontend/dynamic/profile_groups.html', {
        'profile': request.user.profile,
        })

@login_required
@superuser_required
def accept_pi_request(request, pk):

    a = PIRequest.objects.get(pk=pk)

    try:
        group = ResearchGroup.objects.get(name=a.group_name)
    except ResearchGroup.DoesNotExist:
        pass
    else:
        logger.error("Group with that name already exists")
        return HttpResponse(status=403)
    issuer = a.issuer
    group = ResearchGroup.objects.create(name=a.group_name, PI=issuer)
    group.save()
    issuer.is_PI = True
    issuer.save()

    a.delete()

    return HttpResponse(status=200)

@login_required
@superuser_required
def deny_pi_request(request, pk):

    a = PIRequest.objects.get(pk=pk)
    a.delete()

    return HttpResponse(status=200)

@login_required
@superuser_required
def manage_pi_requests(request):
    reqs = PIRequest.objects.all()

    return render(request, 'frontend/manage_pi_requests.html', {
        'profile': request.user.profile,
        'reqs': reqs,
        })

@login_required
def conformer_table(request, pk):
    id = str(pk)
    try:
        e = Ensemble.objects.get(pk=id)
    except Ensemble.DoesNotExist:
        return HttpResponse(status=403)
    profile = request.user.profile

    if e.parent_molecule.project.author != profile and not profile_intersection(profile, e.parent_molecule.project.author):
        return HttpResponse(status=403)

    return render(request, 'frontend/dynamic/conformer_table.html', {
            'profile': request.user.profile,
            'ensemble': e,
        })

@login_required
def conformer_table_post(request):
    if request.method == 'POST':
        try:
            id = int(clean(request.POST['ensemble_id']))
            p_id = int(clean(request.POST['param_id']))
        except KeyError:
            return HttpResponse(status=204)

        try:
            e = Ensemble.objects.get(pk=id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)
        profile = request.user.profile

        if e.parent_molecule.project.author != profile and not profile_intersection(profile, e.parent_molecule.project.author):
            return HttpResponse(status=403)
        try:
            p = Parameters.objects.get(pk=p_id)
        except Parameters.DoesNotExist:
            return HttpResponse(status=403)

        full_summary, hashes = e.ensemble_summary

        if p.md5 in full_summary.keys():
            summary = full_summary[p.md5]

            fms = profile.pref_units_format_string

            rel_energies = [fms.format(i) for i in np.array(summary[5])*profile.unit_conversion_factor]
            structures = [e.structure_set.get(pk=i) for i in summary[4]]
            data = zip(structures, summary[2], rel_energies, summary[6])
            data = sorted(data, key=lambda i: i[0].number)

        else:
            blank = ['-' for i in range(e.structure_set.count())]
            structures = e.structure_set.all()
            data = zip(structures, blank, blank, blank)
            data = sorted(data, key=lambda i: i[0].number)
        return render(request, 'frontend/dynamic/conformer_table.html', {
                'profile': request.user.profile,
                'data': data,
            })

    else:
        return HttpResponse(status=403)

@login_required
def uvvis(request, pk):
    calc = Calculation.objects.get(pk=pk)

    profile = request.user.profile

    if calc.order.author != profile and not profile_intersection(profile, calc.order.author):
        return HttpResponse(status=403)

    spectrum_file = os.path.join(CALCUS_RESULTS_HOME, str(calc.id), "uvvis.csv")

    if os.path.isfile(spectrum_file):
        with open(spectrum_file, 'rb') as f:
            response = HttpResponse(f, content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename={}.csv'.format(id)
            return response
    else:
        return HttpResponse(status=204)

@login_required
def get_calc_data(request, pk):
    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if calc.order.author != profile and not profile_intersection(profile, calc.order.author):
        return HttpResponse(status=403)

    if calc.status == 0:
        return HttpResponse(status=204)

    return format_frames(calc, profile)

def format_frames(calc, profile):
    if calc.status == 1:
        analyse_opt(calc.id)

    multi_xyz = ""
    scan_energy = "Frame,Relative Energy\n"
    RMSD = "Frame,RMSD\n"

    scan_frames = []
    scan_energies = []

    for f in calc.calculationframe_set.values('xyz_structure', 'number', 'RMSD', 'converged', 'energy').order_by('number').all():
        multi_xyz += f['xyz_structure']
        RMSD += "{},{}\n".format(f['number'], f['RMSD'])
        if f['converged'] == True:
            scan_frames.append(f['number'])
            scan_energies.append(f['energy'])

    if len(scan_frames) > 0:
        scan_min = min(scan_energies)
        for n, E in zip(scan_frames, scan_energies):
            scan_energy += "{},{}\n".format(n, (E-scan_min)*profile.unit_conversion_factor)
    return HttpResponse(multi_xyz + ';' + RMSD + ';' + scan_energy)

@login_required
@throttle(zone='load_remote_log')
def get_calc_data_remote(request, pk):
    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if calc.order.author != profile:
        return HttpResponse(status=403)

    if calc.status == 0:
        return HttpResponse(status=204)

    if calc.parameters.software == "Gaussian":
        try:
            os.remove(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log"))
        except OSError:
            pass

        send_cluster_command("load_log\n{}\n{}\n".format(calc.id, calc.order.resource.id))

        ind = 0
        while not os.path.isfile(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")) and ind < 60:
            time.sleep(1)
            ind += 1

        if not os.path.isfile(os.path.join(CALCUS_SCR_HOME, str(calc.id), "calc.log")):
            return HttpResponse(status=404)
    else:
        logger.error("Not implemented")
        return HttpResponse(status=403)

    return format_frames(calc, profile)

def get_calc_frame(request, cid, fid):
    try:
        calc = Calculation.objects.get(pk=cid)
    except Calculation.DoesNotExist:
        return redirect('/home/')

    profile = request.user.profile

    if not can_view_calculation(calc, profile):
        return HttpResponse(status=403)

    if calc.status == 0:
        return HttpResponse(status=204)

    xyz = calc.calculationframe_set.get(number=fid).xyz_structure
    return HttpResponse(xyz)

@login_required
def get_cube(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))
        orb = int(clean(request.POST['orb']))
        calc = Calculation.objects.get(pk=id)

        profile = request.user.profile

        if calc.order.author != profile and not profile_intersection(profile, calc.order.author):
            return HttpResponse(status=403)

        if orb == 0:
            cube_file = "in-HOMO.cube"
        elif orb == 1:
            cube_file = "in-LUMO.cube"
        elif orb == 2:
            cube_file = "in-LUMOA.cube"
        elif orb == 3:
            cube_file = "in-LUMOB.cube"
        else:
            return HttpResponse(status=204)
        spectrum_file = os.path.join(CALCUS_RESULTS_HOME, str(id), cube_file)

        if os.path.isfile(spectrum_file):
            with open(spectrum_file, 'r') as f:
                lines = f.readlines()
            return HttpResponse(''.join(lines))
        else:
            return HttpResponse(status=204)
    return HttpResponse(status=204)

@login_required
def nmr(request):
    if request.method != 'POST':
        return HttpResponse(status=404)

    if 'id' in request.POST.keys():
        try:
            e = Ensemble.objects.get(pk=int(clean(request.POST['id'])))
        except Ensemble.DoesNotExist:
            return HttpResponse(status=404)
    else:
        return HttpResponse(status=404)
    if 'p_id' in request.POST.keys():
        try:
            params = Parameters.objects.get(pk=int(clean(request.POST['p_id'])))
        except Parameters.DoesNotExist:
            return HttpResponse(status=404)
    else:
        return HttpResponse(status=404)

    if 'nucleus' in request.POST.keys():
        nucleus = clean(request.POST['nucleus'])
    else:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not profile_intersection(profile, e.parent_molecule.project.author):
        return HttpResponse(status=403)

    if not e.has_nmr(params):
        return HttpResponse(status=204)

    shifts = e.weighted_nmr_shifts(params)

    if nucleus == 'H':
        content = "Shift,Intensity\n-10,0\n0,0\n"
    else:
        content = "Shift,Intensity\n-200,0\n0,0\n"

    for shift in shifts:
        if shift[1] == nucleus:
            if len(shift) == 4:
                content += "{},{}\n".format(-(shift[3]-0.001), 0)
                content += "{},{}\n".format(-shift[3], 1)
                content += "{},{}\n".format(-(shift[3]+0.001), 0)

    response = HttpResponse(content, content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename={}.csv'.format(id)
    return response

@login_required
def ir_spectrum(request, pk):
    id = str(pk)
    try:
        calc = Calculation.objects.get(pk=id)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if calc.order.author != profile and not profile_intersection(profile, calc.order.author):
        return HttpResponse(status=403)

    spectrum_file = os.path.join(CALCUS_RESULTS_HOME, id, "IR.csv")

    if os.path.isfile(spectrum_file):
        with open(spectrum_file, 'rb') as f:
            response = HttpResponse(f, content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename={}.csv'.format(id)
            return response
    else:
        return HttpResponse(status=204)

@login_required
def vib_table(request, pk):
    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if calc.order.author != profile and not profile_intersection(profile, calc.order.author):
        return HttpResponse(status=403)

    vib_file = os.path.join(CALCUS_RESULTS_HOME, str(calc.id), "vibspectrum")
    orca_file = os.path.join(CALCUS_RESULTS_HOME, str(calc.id), "orcaspectrum")

    vibs = []

    if os.path.isfile(vib_file):
        with open(vib_file) as f:
            lines = f.readlines()

        for line in lines:
            if len(line.split()) > 4 and line[0] != '#':
                sline = line.split()
                try:
                    a = float(sline[1])
                    if a == 0.:
                        continue
                except ValueError:
                    pass
                vib = float(line[20:33].strip())
                vibs.append(vib)

    elif os.path.isfile(orca_file):
        with open(orca_file) as f:
            lines = f.readlines()

        for line in lines:
            vibs.append(line.strip())
    else:
        return HttpResponse(status=204)

    response = ""
    for ind, vib in enumerate(vibs):
        response += '<div class="column is-narrow"><a class="button" id="vib_mode_{}" onclick="animate_vib({});">{}</a></div>'.format(ind, ind, vib)

    return HttpResponse(response)

@login_required
def apply_pi(request):
    if request.method == 'POST':
        profile = request.user.profile

        if profile.is_PI:
            return render(request, 'frontend/apply_pi.html', {
                'profile': request.user.profile,
                'message': "You are already a PI!",
            })
        group_name = clean(request.POST['group_name'])
        req = PIRequest.objects.create(issuer=profile, group_name=group_name, date_issued=timezone.now())
        return render(request, 'frontend/apply_pi.html', {
            'profile': request.user.profile,
            'message': "Your request has been received.",
        })
    else:
        return HttpResponse(status=403)

@login_required
def info_table(request, pk):
    id = str(pk)
    calc = Calculation.objects.get(pk=id)

    profile = request.user.profile

    if calc not in profile.calculation_set.all() and not profile_intersection(profile, calc.author):
        return HttpResponse(status=403)

    return render(request, 'frontend/dynamic/info_table.html', {
            'profile': request.user.profile,
            'calculation': calc,
        })

@login_required
def next_step(request, pk):
    id = str(pk)
    calc = Calculation.objects.get(pk=id)

    profile = request.user.profile

    if calc not in profile.calculation_set.all() and not profile_intersection(profile, calc.author):
        return HttpResponse(status=403)

    return render(request, 'frontend/dynamic/next_step.html', {
            'calculation': calc,
        })


@login_required
def download_structures(request, ee):
    try:
        e = Ensemble.objects.get(pk=ee)
    except Ensemble.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_ensemble(e, profile):
        return HttpResponse(status=404)

    structs = ""
    for s in e.structure_set.all():
        if s.xyz_structure == "":
            structs += "1\nMissing Structure\nC 0 0 0"
            logger.warning("Missing structure! ({}, {})".format(profile.username, ee))
        structs += s.xyz_structure

    response = HttpResponse(structs)
    response['Content-Type'] = 'text/plain'
    response['Content-Disposition'] = 'attachment; filename={}.xyz'.format(ee)
    return response

@login_required
def download_structure(request, ee, num):
    try:
        e = Ensemble.objects.get(pk=ee)
    except Ensemble.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_ensemble(e, profile):
        return HttpResponse(status=404)

    try:
        s = e.structure_set.get(number=num)
    except Structure.DoesNotExist:
        return HttpResponse(status=404)

    response = HttpResponse(s.xyz_structure)
    response['Content-Type'] = 'text/plain'
    response['Content-Disposition'] = 'attachment; filename={}_conf{}.xyz'.format(ee, num)
    return response

def gen_3D(request):
    if request.method == 'POST':
        mol = request.POST['mol']
        clean_mol = clean(mol)

        t = time.time()
        with open("/tmp/{}.mol".format(t), 'w') as out:
            out.write(clean_mol)

        system("obabel /tmp/{}.mol -O /tmp/{}.xyz -h --gen3D".format(t, t), force_local=True)
        with open("/tmp/{}.xyz".format(t)) as f:
            lines = f.readlines()
        if ''.join(lines).strip() == '':
            return HttpResponse(status=404)

        return HttpResponse(lines)
    return HttpResponse(status=403)

@login_required
def rename_molecule(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            mol = Molecule.objects.get(pk=id)
        except Molecule.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if mol.project.author != profile:
            return HttpResponse(status=403)

        if 'new_name' in request.POST.keys():
            name = clean(request.POST['new_name'])

        if name.strip() == "":
            name = "Nameless molecule"

        mol.name = name
        mol.save()
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)

@login_required
def rename_project(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            proj = Project.objects.get(pk=id)
        except Project.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if proj.author != profile:
            return HttpResponse(status=403)

        if 'new_name' in request.POST.keys():
            name = clean(request.POST['new_name'])

        if name.strip() == "":
            name = "Nameless project"

        proj.name = name
        proj.save()
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)

@login_required
def toggle_private(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            proj = Project.objects.get(pk=id)
        except Project.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if proj.author != profile:
            return HttpResponse(status=403)

        if 'val' in request.POST.keys():
            try:
                val = int(clean(request.POST['val']))
            except ValueError:
                return HttpResponse(status=403)
        else:
            return HttpResponse(status=403)

        if val not in [0, 1]:
            return HttpResponse(status=403)

        proj.private = val
        proj.save()

        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)

@login_required
def toggle_flag(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            e = Ensemble.objects.get(pk=id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if e.parent_molecule.project.author != profile:
            return HttpResponse(status=403)

        if 'val' in request.POST.keys():
            try:
                val = int(clean(request.POST['val']))
            except ValueError:
                return HttpResponse(status=403)
        else:
            return HttpResponse(status=403)

        if val not in [0, 1]:
            return HttpResponse(status=403)

        e.flagged = val
        e.save()

        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)


@login_required
def rename_ensemble(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            e = Ensemble.objects.get(pk=id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if e.parent_molecule.project.author != profile:
            return HttpResponse(status=403)

        if 'new_name' in request.POST.keys():
            name = clean(request.POST['new_name'])

        if name.strip() == "":
            name = "Nameless ensemble"

        e.name = name
        e.save()
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)

@login_required
def rename_folder(request):
    if request.method == 'POST':
        id = int(clean(request.POST['id']))

        try:
            f = Folder.objects.get(pk=id)
        except Folder.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if f.project.author != profile:
            return HttpResponse(status=403)

        if 'new_name' in request.POST.keys():
            name = clean(request.POST['new_name'])

        if name.strip() == "":
            return HttpResponse(status=403)

        f.name = name
        f.save()
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=403)


@login_required
def get_structure(request):
    if request.method == 'POST':
        try:
            id = int(clean(request.POST['id']))
        except ValueError:
            return HttpResponse(status=404)

        try:
            e = Ensemble.objects.get(pk=id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=403)

        profile = request.user.profile

        if not can_view_ensemble(e, profile):
            return HttpResponse(status=403)

        structs = e.structure_set.all()

        if len(structs) == 0:
            return HttpResponse(status=204)

        if 'num' in request.POST.keys():
            num = int(clean(request.POST['num']))
            try:
                struct = structs.get(number=num)
            except Structure.DoesNotExist:
                inds = [i.number for i in structs]
                m = inds.index(min(inds))
                return HttpResponse(structs[m].xyz_structure)

            else:
                return HttpResponse(struct.xyz_structure)
        else:
            inds = [i.number for i in structs]
            m = inds.index(min(inds))
            return HttpResponse(structs[m].xyz_structure)

@login_required
def get_vib_animation(request):
    if request.method == 'POST':
        url = clean(request.POST['id'])
        try:
            id = int(url.split('/')[-1])
        except ValueError:
            return HttpResponse(status=404)

        try:
            calc = Calculation.objects.get(pk=id)
        except Calculation.DoesNotExist:
            return HttpResponse(status=404)

        profile = request.user.profile

        if not can_view_calculation(calc, profile):
            return HttpResponse(status=403)

        num = int(clean(request.POST['num']))
        expected_file = os.path.join(CALCUS_RESULTS_HOME, str(id), "freq_{}.xyz".format(num))
        if os.path.isfile(expected_file):
            with open(expected_file) as f:
                lines = f.readlines()

            return HttpResponse(''.join(lines))
        else:
            return HttpResponse(status=204)

@login_required
def get_scan_animation(request):
    if request.method == 'POST':
        url = clean(request.POST['id'])
        id = int(url.split('/')[-1])

        try:
            calc = Calculation.objects.get(pk=id)
        except Calculation.DoesNotExist:
            return HttpResponse(status=404)

        profile = request.user.profile

        if not can_view_calculation(calc, profile):
            return HttpResponse(status=403)

        type = calc.type

        if type != 5:
            return HttpResponse(status=403)

        expected_file = os.path.join(CALCUS_RESULTS_HOME, id, "xtbscan.xyz")
        if os.path.isfile(expected_file):
            with open(expected_file) as f:
                lines = f.readlines()

            inds = []
            num_atoms = lines[0]

            for ind, line in enumerate(lines):
                if line == num_atoms:
                    inds.append(ind)

            inds.append(len(lines)-1)
            xyz_files = []
            for ind, _ in enumerate(inds[1:]):
                xyz = ""
                for _ind in range(inds[ind-1], inds[ind]):
                    if lines[_ind].strip() != '':
                        xyz += lines[_ind].strip() + '\\n'
                xyz_files.append(xyz)
            return render(request, 'frontend/scan_animation.html', {
                'xyz_files': xyz_files[1:]
                })
        else:
            return HttpResponse(status=204)

@login_required
def download_log(request, pk):
    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_calculation(calc, profile):
        return HttpResponse(status=403)

    if calc.status == 2 or calc.status == 3:
        dir = os.path.join(CALCUS_RESULTS_HOME, str(pk))
    elif calc.status == 1:
        dir = os.path.join(CALCUS_SCR_HOME, str(pk))
    elif calc.status == 0:
        return HttpResponse(status=204)

    logs = glob.glob(dir + '/*.out')
    logs += glob.glob(dir + '/*.log')

    if len(logs) > 1:
        mem = BytesIO()
        with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zip:
            for f in logs:
                zip.write(f, "{}_".format(calc.id) + basename(f))

        response = HttpResponse(mem.getvalue(), content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename="calc_{}.zip"'.format(calc.id)
        return response
    elif len(logs) == 1:
        with open(logs[0]) as f:
            lines = f.readlines()
            response = HttpResponse(''.join(lines), content_type='text/plain')
            response['Content-Disposition'] = 'attachment; filename="calc_{}.log"'.format(calc.id)
            return response
    else:
        logger.warning("No log to download! (Calculation {})".format(pk))
        return HttpResponse(status=404)

@login_required
def download_all_logs(request, pk):
    try:
        order = CalculationOrder.objects.get(pk=pk)
    except CalculationOrder.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_order(order, profile):
        return HttpResponse(status=403)

    order_logs = {}
    for calc in order.calculation_set.all():
        if calc.status == 2 or calc.status == 3:
            dir = os.path.join(CALCUS_RESULTS_HOME, str(calc.id))
        elif calc.status == 1:
            dir = os.path.join(CALCUS_SCR_HOME, str(calc.id))
        elif calc.status == 0:
            return HttpResponse(status=204)

        logs = glob.glob(dir + '/*.out')
        logs += glob.glob(dir + '/*.log')

        order_logs[calc.id] = logs

    mem = BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zip:
        for c in order_logs.keys():
            for f in order_logs[c]:
                zip.write(f, "{}_".format(c) + basename(f))

    response = HttpResponse(mem.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="order_{}.zip"'.format(pk)
    return response

@login_required
def log(request, pk):
    LOG_HTML = """
    <label class="label">{}</label>
    <textarea class="textarea" style="height: 300px;" readonly>
    {}
    </textarea>
    """

    response = ''

    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_calculation(calc, profile):
        return HttpResponse(status=403)

    if calc.status == 2 or calc.status == 3:
        dir = os.path.join(CALCUS_RESULTS_HOME, str(pk))
    elif calc.status == 1:
        dir = os.path.join(CALCUS_SCR_HOME, str(pk))
    elif calc.status == 0:
        return HttpResponse(status=204)

    for out in glob.glob(dir + '/*.out'):
        out_name = out.split('/')[-1]
        with open(out) as f:
            lines = f.readlines()
        response += LOG_HTML.format(out_name, ''.join(lines))

    for log in glob.glob(dir + '/*.log'):
        log_name = log.split('/')[-1]
        with open(log) as f:
            lines = f.readlines()
        response += LOG_HTML.format(log_name, ''.join(lines))

    return HttpResponse(response)

@login_required
def manage_access(request, pk):
    access = ClusterAccess.objects.get(pk=pk)

    profile = request.user.profile

    if access.owner != profile:
        return HttpResponse(status=403)

    return render(request, 'frontend/manage_access.html', {
            'profile': request.user.profile,
            'access': access,
        })

@login_required
def owned_accesses(request):
    return render(request, 'frontend/dynamic/owned_accesses.html', {
            'profile': request.user.profile,
        })

@login_required
def profile(request):
    return render(request, 'frontend/profile.html', {
            'profile': request.user.profile,
        })

@login_required
def update_preferences(request):
    if request.method == 'POST':
        profile = request.user.profile

        if 'pref_units' not in request.POST.keys():
            return HttpResponse(status=204)


        if 'default_gaussian' in request.POST.keys():
            default_gaussian = clean(request.POST['default_gaussian']).replace('\n', '')
            profile.default_gaussian = default_gaussian

        if 'default_orca' in request.POST.keys():
            default_orca = clean(request.POST['default_orca']).replace('\n', '')
            profile.default_orca = default_orca

        units = clean(request.POST['pref_units'])

        try:
            unit_code = profile.INV_UNITS[units]
        except KeyError:
            return HttpResponse(status=204)

        profile.pref_units = unit_code
        profile.save()
        return HttpResponse(status=200)
    else:
        return HttpResponse(status=404)

@login_required
def launch(request):
    profile = request.user.profile
    params = {
            'profile': profile,
            'procs': BasicStep.objects.all().order_by(Lower('name')),
            'allow_local_calc': settings.ALLOW_LOCAL_CALC,
            'packages': settings.PACKAGES,
        }

    if 'ensemble' in request.POST.keys():
        try:
            e = Ensemble.objects.get(pk=clean(request.POST['ensemble']))
        except Ensemble.DoesNotExist:
            return redirect('/home/')

        if not can_view_ensemble(e, profile):
            return HttpResponse(status=403)

        o = False
        if e.result_of.first() is not None:
            o = e.result_of.first()
        elif e.calculationorder_set.first() is not None:# e.g. SP on uploaded file
            o = e.calculationorder_set.first()

        if o and o.resource is not None:
            params['resource'] = o.resource.cluster_address

        params['ensemble'] = e
        if 'structures' in request.POST.keys():
            s_str = clean(request.POST['structures'])
            s_nums = [int(i) for i in s_str.split(',')]

            try:
                struct = e.structure_set.get(number=s_nums[0])
            except Structure.DoesNotExist:
                return HttpResponse(status=404)

            avail_nums = [i['number'] for i in e.structure_set.values('number')]

            for s_num in s_nums:
                if s_num not in avail_nums:
                    return HttpResponse(status=404)

            init_params = struct.properties.all()[0].parameters

            params['structures'] = s_str
            params['structure'] = struct
            params['init_params_id'] = init_params.id
        else:
            init_params = e.structure_set.all()[0].properties.all()[0].parameters

            params['init_params_id'] = init_params.id
    elif 'calc_id' in request.POST.keys():
        calc_id = int(clean(request.POST['calc_id']))

        if 'frame_num' not in request.POST.keys():
            return HttpResponse(status=404)

        frame_num = int(clean(request.POST['frame_num']))

        try:
            calc = Calculation.objects.get(pk=calc_id)
        except Calculation.DoesNotExist:
            return redirect('/home/')

        if not can_view_calculation(calc, profile):
            return HttpResponse(status=403)

        if calc.order.resource is not None:
            params['resource'] = calc.order.resource.cluster_address

        try:
            frame = calc.calculationframe_set.get(number=frame_num)
        except CalculationFrame.DoesNotExist:
            return redirect('/home/')

        init_params = calc.order.parameters

        params['calc'] = calc
        params['frame_num'] = frame_num
        params['init_params_id'] = init_params.id

    return render(request, 'frontend/launch.html', params)


@login_required
def launch_project(request, pk):
    try:
        proj = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return redirect('/home/')

    profile = request.user.profile

    if not can_view_project(proj, profile):
        return HttpResponse(status=403)

    if proj.preset is not None:
        init_params_id = proj.preset.id

        return render(request, 'frontend/launch.html', {
                'proj': proj,
                'profile': request.user.profile,
                'procs': BasicStep.objects.all(),
                'init_params_id': init_params_id,
                'allow_local_calc': settings.ALLOW_LOCAL_CALC,
                'packages': settings.PACKAGES,
            })
    else:
        return render(request, 'frontend/launch.html', {
                'proj': proj,
                'profile': request.user.profile,
                'procs': BasicStep.objects.all(),
                'allow_local_calc': settings.ALLOW_LOCAL_CALC,
                'packages': settings.PACKAGES,
            })

@login_required
def check_functional(request):
    if 'functional' not in request.POST.keys():
        return HttpResponse(status=400)

    func = clean(request.POST['functional'])

    if func.strip() == "":
        return HttpResponse("")

    try:
        ccinput.utilities.get_abs_method(func)
    except ccinput.exceptions.InvalidParameter:
        return HttpResponse("Unknown functional")

    return HttpResponse("")

@login_required
def check_basis_set(request):
    if 'basis_set' not in request.POST.keys():
        return HttpResponse(status=400)

    bs = clean(request.POST['basis_set'])

    if bs.strip() == "":
        return HttpResponse("")

    try:
        ccinput.utilities.get_abs_basis_set(bs)
    except ccinput.exceptions.InvalidParameter:
        return HttpResponse("Unknown basis set")

    return HttpResponse("")

@login_required
def check_solvent(request):
    if 'solvent' not in request.POST:
        return HttpResponse(status=400)

    solv = clean(request.POST['solvent'])

    if solv.strip() == "" or solv.strip().lower() == "vacuum":
        return HttpResponse("")

    if 'software' not in request.POST:
        return HttpResponse(status=400)

    software = clean(request.POST['software']).lower()

    if software != 'xtb': # To change once xtb is supported by ccinput
        try:
            software = ccinput.utilities.get_abs_software(software)
        except ccinput.exceptions.InvalidParameter:
            return HttpResponse(status=400)

    try:
        solvent = ccinput.utilities.get_abs_solvent(solv)
    except ccinput.exceptions.InvalidParameter:
        return HttpResponse("Unknown solvent")

    if solvent not in ccinput.constants.SOFTWARE_SOLVENTS[software]:
        return HttpResponse("Unknown solvent")
    else:
        return HttpResponse("")

@login_required
def delete_preset(request, pk):
    try:
        p = Preset.objects.get(pk=pk)
    except Preset.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_preset(p, profile):
        return HttpResponse(status=403)

    p.delete()
    return HttpResponse("Preset deleted")

@login_required
def launch_presets(request):
    profile = request.user.profile

    presets = profile.preset_set.all().order_by("name")
    return render(request, 'frontend/dynamic/launch_presets.html', { 'presets': presets })

@login_required
def load_preset(request, pk):
    try:
        p = Preset.objects.get(pk=pk)
    except Preset.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile

    if not can_view_preset(p, profile):
        return HttpResponse(status=403)

    return render(request, 'frontend/dynamic/load_params.js', {
            'params': p.params,
            'load_charge': False,
        })

@login_required
def load_params(request, pk):
    try:
        params = Parameters.objects.get(pk=pk)
    except Parameters.DoesNotExist:
        return HttpResponse(status=404)

    if not can_view_parameters(params, request.user.profile):
        return HttpResponse(status=403)

    return render(request, 'frontend/dynamic/load_params.js', {
            'params': params,
            'load_charge': True,
        })

class CsvParameters:
    def __init__(self):
        self.molecules = {}

class CsvMolecule:
    def __init__(self):
        self.ensembles = {}
        self.name = ""

class CsvEnsemble:
    def __init__(self):
        self.name = ""
        self.data = []

def get_csv(proj, profile, scope="flagged", details="full", folders=True):
    pref_units = profile.pref_units
    units = profile.pref_units_name

    if pref_units == 0:
        CONVERSION = HARTREE_FVAL
        structure_str = ",,,{},{},{:.1f},{:.1f},{:.3f},{:.1f}\n"
        ensemble_str = "{:.1f}"
    elif pref_units == 1:
        CONVERSION = HARTREE_TO_KCAL_F
        structure_str = ",,,{},{},{:.2f},{:.2f},{:.3f},{:.2f}\n"
        ensemble_str = "{:.2f}"
    elif pref_units == 2:
        CONVERSION = 1
        structure_str = ",,,{},{},{:.7f},{:.7f},{:.3f},{:.7f}\n"
        ensemble_str = "{:.7f}"

    summary = {}
    hashes = {}
    csv = ""

    if folders:
        def get_folder_data(folder):
            subfolders = folder.folder_set.all()
            ensembles = folder.ensemble_set.filter(flagged=True)

            ensemble_data = {}
            folder_data = {}

            for e in ensembles:
                if details == "full":
                    summ, ehashes = e.ensemble_summary
                else:
                    summ, ehashes = e.ensemble_short_summary

                for hash, long_name in ehashes.items():
                    if hash not in hashes.keys():
                        hashes[hash] = long_name
                ensemble_data["{} - {}".format(e.parent_molecule.name, e.name)] = summ

            for f in subfolders:
                folder_data[f.name] = get_folder_data(f)

            return [ensemble_data, folder_data]

        main_folder = proj.main_folder
        if main_folder is None:
            raise Exception("Main folder of project {} is null!".format(proj.id))

        data = get_folder_data(main_folder)

        def format_data(ensemble_data, folder_data, hash):
            _str = ""
            for ename, edata in sorted(ensemble_data.items(), key=lambda a: a[0]):
                if hash in edata.keys():
                    nums, degens, energies, free_energies, ids, rel_energies, weights, w_e, w_f_e  = edata[hash]
                    if isinstance(w_f_e, float):
                        _w_f_e = ensemble_str.format(w_f_e*CONVERSION)
                    else:
                        _w_f_e = w_f_e
                    _w_e = ensemble_str.format(w_e*CONVERSION)
                    _str += "{},{},{}\n".format(ename, _w_e, _w_f_e)

            for fname, f in sorted(folder_data.items(), key=lambda a: a[0]):
                _str += "\n,{},Energy,Free Energy\n".format(fname)

                f_str = format_data(*f, hash)
                _str += '\n'.join([',' + i for i in f_str.split('\n')])
            return _str

        main_str = ""
        for i, n in hashes.items():
            main_str += "{}\n".format(n)
            main_str += format_data(*data, i)
            main_str += "\n\n"
        return main_str
    else:
        molecules = list(proj.molecule_set.prefetch_related('ensemble_set').all())
        for mol in molecules:
            if scope == "flagged":
                ensembles = mol.ensemble_set.filter(flagged=True)
            else:
                ensembles = mol.ensemble_set.all()

            for e in ensembles:
                if details == "full":
                    summ, ehashes = e.ensemble_summary
                else:
                    summ, ehashes = e.ensemble_short_summary

                for hash, long_name in ehashes.items():
                    if hash not in hashes.keys():
                        hashes[hash] = long_name

                for p_name in summ.keys():
                    if p_name in summary.keys():
                        csv_p = summary[p_name]
                        if mol.id in csv_p.molecules.keys():
                            csv_mol = csv_p.molecules[mol.id]

                            csv_e = CsvEnsemble()
                            csv_e.name = e.name
                            csv_e.data = summ[p_name]
                            csv_mol.ensembles[e.id] = csv_e

                        else:
                            csv_mol = CsvMolecule()
                            csv_mol.name = mol.name
                            csv_p.molecules[mol.id] = csv_mol

                            csv_e = CsvEnsemble()
                            csv_e.name = e.name
                            csv_e.data = summ[p_name]
                            csv_mol.ensembles[e.id] = csv_e
                    else:
                        csv_p = CsvParameters()

                        csv_mol = CsvMolecule()
                        csv_mol.name = mol.name

                        csv_e = CsvEnsemble()
                        csv_e.name = e.name
                        csv_e.data = summ[p_name]

                        csv_mol.ensembles[e.id] = csv_e

                        csv_p.molecules[mol.id] = csv_mol
                        summary[p_name] = csv_p

        if details == 'full':
            csv += "Parameters,Molecule,Ensemble,Structure\n"
            for p_name in summary.keys():
                p = summary[p_name]
                csv += "{},\n".format(hashes[p_name])
                for mol in p.molecules.values():
                    csv += ",{},\n".format(mol.name)
                    csv += ",,,Number,Degeneracy,Energy,Relative Energy,Weight,Free Energy,\n"
                    for e in mol.ensembles.values():
                        csv += ",,{},\n".format(e.name)
                        nums, degens, energies, free_energies, ids, rel_energies, weights, w_e, w_f_e = e.data
                        for n, d, en, f_en, r_el, w in zip(nums, degens, energies, free_energies, rel_energies, weights):
                            csv += structure_str.format(n, d, en*CONVERSION, r_el*CONVERSION, w, f_en*CONVERSION)
        csv += "\n\n"
        csv += "SUMMARY\n"
        csv += "Method,Molecule,Ensemble,Weighted Energy ({}),Weighted Free Energy ({}),\n".format(units, units)
        for p_name in summary.keys():
            p = summary[p_name]
            csv += "{},\n".format(hashes[p_name])
            for mol in p.molecules.values():
                csv += ",{},\n".format(mol.name)
                for e in mol.ensembles.values():
                    if details == "full":
                        arr_ind = 7
                    else:
                        arr_ind = 0

                    _w_e = e.data[arr_ind]
                    if _w_e != '-':
                        w_e = ensemble_str.format(_w_e*CONVERSION)
                    else:
                        w_e = _w_e

                    _w_f_e = e.data[arr_ind+1]
                    if _w_f_e != '-':
                        w_f_e = ensemble_str.format(_w_f_e*CONVERSION)
                    else:
                        w_f_e = _w_f_e

                    csv += ",,{},{},{}\n".format(e.name, w_e, w_f_e)

    return csv

def download_project_csv(proj, profile, scope, details, folders):

    csv = get_csv(proj, profile, scope, details, folders)

    proj_name = proj.name.replace(' ', '_')
    response = HttpResponse(csv, content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename={}.csv'.format(proj_name)
    return response

@login_required
def cancel_calc(request):

    if request.method != "POST":
        return HttpResponse(status=403)

    profile = request.user.profile

    if 'id' in request.POST.keys():
        try:
            id = int(clean(request.POST['id']))
        except ValueError:
            return HttpResponse(status=404)

    try:
        calc = Calculation.objects.get(pk=id)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    if profile != calc.order.author:
        return HttpResponse(status=403)

    if is_test:
        cancel(calc.id)
    else:
        cancel.delay(calc.id)

    return HttpResponse(status=200)

def download_project_logs(proj, profile, scope, details, folders):
    #folders options makes this somewhat duplicate code

    tmp_dir = "/tmp/{}_{}_{}".format(profile.username, proj.author.username, time.time())
    os.mkdir(tmp_dir)
    for mol in sorted(proj.molecule_set.all(), key=lambda l: l.name):
        for e in mol.ensemble_set.all():
            if scope == 'flagged' and not e.flagged:
                continue
            e_dir = os.path.join(tmp_dir, str(e.id) + '_' + e.name.replace(' ', '_'))
            try:
                os.mkdir(e_dir)
            except FileExistsError:
                pass
            for ind, s in enumerate(e.structure_set.all()):
                for calc in s.calculation_set.all():
                    if calc.status == 0:
                        continue
                    if details == "freq":
                        if calc.step.name != "Frequency Calculation":
                            continue
                        log_name = e.name + '_' + calc.parameters.file_name + '_conf{}'.format(s.number)
                    elif details == "full":
                        log_name = e.name + '_' + calc.step.name + '_' + calc.parameters.file_name + '_conf{}'.format(s.number)

                    log_name = log_name.replace(' ', '_')
                    try:
                        copyfile(os.path.join(CALCUS_RESULTS_HOME, str(calc.id), "calc.out"), os.path.join(e_dir, log_name + '.log'))
                    except FileNotFoundError:
                        logger.warning("Calculation not found: {}".format(calc.id))
                    if calc.parameters.software == 'xtb':#xtb logs don't contain the structure
                        with open(os.path.join(e_dir, log_name + '.xyz'), 'w') as out:
                            out.write(s.xyz_structure)

    for d in glob.glob("{}/*/".format(tmp_dir)):
        if len(os.listdir(d)) == 0:
            os.rmdir(d)

    mem = BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zip:
        for d in glob.glob("{}/*/".format(tmp_dir)):
            for f in glob.glob("{}*".format(d)):
                zip.write(f, os.path.join(proj.name.replace(' ', '_'), *f.split('/')[3:]))

    response = HttpResponse(mem.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="{}_logs.zip"'.format(proj.name.replace(' ', '_'))
    return response

@login_required
def download_project_post(request):
    if 'id' in request.POST.keys():
        try:
            id = int(clean(request.POST['id']))
        except ValueError:
            return error(request, "Invalid project")
    else:
        return HttpResponse(status=403)

    if 'data' in request.POST.keys():
        data = clean(request.POST['data'])
        if data not in ['summary', 'logs']:
            return error(request, "Invalid data type requested")
    else:
        return error(request, "No data type requested")

    if 'scope' in request.POST.keys():
        scope = clean(request.POST['scope'])
        if scope not in ['all', 'flagged']:
            return error(request, "Invalid scope")
    else:
        return error(request, "No scope given")

    if 'details' in request.POST.keys():
        details = clean(request.POST['details'])
        if data == 'summary' and details not in ['full', 'summary']:
            return error(request, "Invalid details level")
        if data == 'logs' and details not in ['full', 'freq']:
            return error(request, "Invalid details level")
    else:
        return error(request, "No details level given")

    folders = False
    if 'folders' in request.POST.keys():
        folders = clean(request.POST['folders'])
        if folders.lower() == "false":
            folders = False
        elif folders.lower() == "true":
            folders = True
        else:
            return error(request, "Invalid folders option (true or false)")

    try:
        proj = Project.objects.get(pk=id)
    except Project.DoesNotExist:
        return error(request, "Invalid project")

    profile = request.user.profile

    if not profile_intersection(proj.author, profile):
        return HttpResponseRedirect("/home/")

    if data == 'summary':
        return download_project_csv(proj, profile, scope, details, folders)
    elif data == 'logs':
        return download_project_logs(proj, profile, scope, details, folders)

@login_required
def download_project(request, pk):
    try:
        proj = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return HttpResponse(status=403)

    profile = request.user.profile

    if not profile_intersection(proj.author, profile):
        return HttpResponseRedirect("/home/")

    return render(request, 'frontend/download_project.html', {'proj': proj})

@login_required
def project_folders(request, username, proj, folder_path):
    path = clean(folder_path).split('/')

    # Make trailing slashes mandatory
    if path[-1].strip() != '':
        return HttpResponseRedirect("/projects/{}/{}/{}".format(username, proj, folder_path + '/'))

    target_project = clean(proj)
    target_username = clean(username)

    try:
        target_profile = User.objects.get(username=target_username).profile
    except User.DoesNotExist:
        return HttpResponseRedirect("/home/")

    if not profile_intersection(request.user.profile, target_profile):
        return HttpResponseRedirect("/home/")

    try:
        project = target_profile.project_set.get(name=target_project)
    except Project.DoesNotExist:
        return HttpResponseRedirect("/home/")

    if not can_view_project(project, request.user.profile):
        return HttpResponseRedirect("/home/")

    folders = []
    ensembles = []

    def get_subfolder(path, folder):
        if len(path) == 0 or path[0].strip() == '':
            return folder
        name = path.pop(0)

        try:
            subfolder = folder.folder_set.get(name=name)
        except Folder.DoesNotExist:
            return None

        return get_subfolder(path, subfolder)

    if len(path) == 1:
        folder = project.main_folder
    else:
        folder = get_subfolder(path[1:], project.main_folder)

    if folder is None:
        return HttpResponse(status=404)

    folders = folder.folder_set.all().order_by(Lower('name'))
    ensembles = folder.ensemble_set.all().order_by(Lower('name'))

    return render(request, 'frontend/project_folders.html', {
        'project': project,
        'folder': folder,
        'folders': folders,
        'ensembles': ensembles,
    })

@login_required
def download_folder(request, pk):
    try:
        folder = Folder.objects.get(pk=pk)
    except Folder.DoesNotExist:
        return HttpResponse(status=404)

    if not can_view_project(folder.project, request.user.profile):
        return HttpResponse(status=403)

    def add_folder_data(zip, folder, path):
        subfolders = folder.folder_set.all()
        ensembles = folder.ensemble_set.filter(flagged=True)

        for e in ensembles:
            prefix = "{}.{}_".format(e.parent_molecule.name, e.name)
            related_orders = _get_related_calculations(e)
            # Verify if the user can view the ensemble?
            # The ensembles should be in the project which he can view, so probably not necessary

            for o in related_orders:
                for c in o.calculation_set.all():
                    if c.status == 2:
                        name = clean_filename(prefix + c.parameters.file_name + "_" + c.step.short_name + "_conf" + str(c.structure.number) + ".log")
                        zip.write(os.path.join(CALCUS_RESULTS_HOME, str(c.id), "calc.out"), os.path.join(path, clean_filename(folder.name), name))

        for f in subfolders:
            add_folder_data(zip, f, os.path.join(path, clean_filename(folder.name)))

    mem = BytesIO()
    with zipfile.ZipFile(mem, 'w', zipfile.ZIP_DEFLATED) as zip:
        add_folder_data(zip, folder, '')

    response = HttpResponse(mem.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = 'attachment; filename="{}_{}.zip"'.format(clean_filename(folder.project.name), clean_filename(folder.name))
    return response

@login_required
def move_element(request):

    if 'id' not in request.POST.keys():
        return HttpResponse(status=400)

    id = int(clean(request.POST['id']))

    if 'folder_id' not in request.POST.keys():
        return HttpResponse(status=400)

    folder_id = int(clean(request.POST['folder_id']))

    if 'type' not in request.POST.keys():
        return HttpResponse(status=400)

    type = clean(request.POST['type'])

    if type not in ['ensemble', 'folder']:
        return HttpResponse(status=400)

    try:
        folder = Folder.objects.get(pk=folder_id)
    except Folder.DoesNotExist:
        return HttpResponse(status=404)

    if request.user.profile != folder.project.author:
        return HttpResponse(status=403)

    if type == 'ensemble':
        try:
            e = Ensemble.objects.get(pk=id)
        except Ensemble.DoesNotExist:
            return HttpResponse(status=404)

        if request.user.profile != e.parent_molecule.project.author:
            return HttpResponse(status=404)

        if not e.flagged:
            return HttpResponse(status=400)

        if e.folder != folder:
            e.folder = folder
            e.save()
    elif type == 'folder':
        if folder.depth > MAX_FOLDER_DEPTH:
            return HttpResponse(status=403)

        try:
            f = Folder.objects.get(pk=id)
        except Folder.DoesNotExist:
            return HttpResponse(status=404)

        if request.user.profile != f.project.author:
            return HttpResponse(status=403)

        if f.parent_folder != folder:
            f.parent_folder = folder
            f.depth = folder.depth + 1
            f.save()

    return HttpResponse(status=204)

@login_required
def relaunch_calc(request):
    if request.method != "POST":
        return HttpResponse(status=403)

    profile = request.user.profile

    if 'id' in request.POST.keys():
        try:
            id = int(clean(request.POST['id']))
        except ValueError:
            return HttpResponse(status=404)

    try:
        calc = Calculation.objects.get(pk=id)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    if profile != calc.order.author:
        return HttpResponse(status=403)

    if calc.status != 3:
        return HttpResponse(status=204)

    scr_dir = os.path.join(CALCUS_SCR_HOME, str(calc.id))
    res_dir = os.path.join(CALCUS_RESULTS_HOME, str(calc.id))

    try:
        rmtree(scr_dir)
    except FileNotFoundError:
        pass
    try:
        rmtree(res_dir)
    except FileNotFoundError:
        pass

    calc.status = 0
    calc.remote_id = 0
    calc.order.hidden = False
    calc.order.save()
    calc.save()

    if calc.local:
        t = run_calc.s(calc.id).set(queue='comp')
        res = t.apply_async()
        calc.task_id = res
        calc.save()
    else:
        send_cluster_command("launch\n{}\n{}\n".format(calc.id, calc.order.resource_id))

    return HttpResponse(status=200)

@login_required
def refetch_calc(request):
    if request.method != "POST":
        return HttpResponse(status=403)

    profile = request.user.profile

    if 'id' in request.POST.keys():
        try:
            id = int(clean(request.POST['id']))
        except ValueError:
            return HttpResponse(status=404)

    try:
        calc = Calculation.objects.get(pk=id)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    if profile != calc.order.author:
        return HttpResponse(status=403)

    if calc.status != 3:
        return HttpResponse(status=204)

    if calc.local:
        return HttpResponse(status=204)

    calc.status = 1
    calc.save()

    send_cluster_command("launch\n{}\n{}\n".format(calc.id, calc.order.resource_id))

    return HttpResponse(status=200)

@login_required
def ensemble_map(request, pk):
    try:
        mol = Molecule.objects.get(pk=pk)
    except Molecule.DoesNotExist:
        return redirect('/home/')

    profile = request.user.profile
    if not can_view_molecule(mol, profile):
        return redirect('/home/')
    json = """{{
                "nodes": [
                        {}
                        ],
                "edges": [
                        {}
                    ]
                }}"""
    nodes = ""
    for e in mol.ensemble_set.all():
        if e.flagged:
            border_text = """, "bcolor": "black", "bwidth": 2"""
        else:
            border_text = ""
        nodes += """{{ "data": {{"id": "{}", "name": "{}", "href": "/ensemble/{}", "color": "{}"{}}} }},""".format(e.id, e.name, e.id, e.get_node_color, border_text)
    nodes = nodes[:-1]

    edges = ""
    for e in mol.ensemble_set.all():
        if e.origin != None:
            edges += """{{ "data": {{"source": "{}", "target": "{}"}} }},""".format(e.origin.id, e.id)
    edges = edges[:-1]
    response = HttpResponse(json.format(nodes, edges), content_type='text/json')

    return HttpResponse(response)

@login_required
def analyse(request, project_id):
    profile = request.user.profile

    try:
        proj = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        return HttpResponse(status=403)

    if not can_view_project(proj, profile):
        return HttpResponse(status=403)

    csv = get_csv(proj, profile, folders=False)
    js_csv = []
    for ind1, line in enumerate(csv.split('\n')):
        for ind2, el in enumerate(line.split(',')):
            js_csv.append([el, ind1, ind2])
    l = len(csv.split('\n')) + 5
    return render(request, 'frontend/analyse.html', {'data': js_csv, 'len': l, 'proj': proj})

@login_required
def calculationorder(request, pk):
    try:
        order = CalculationOrder.objects.get(pk=pk)
    except CalculationOrder.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile
    if not can_view_order(order, profile):
        return HttpResponse(status=404)

    return render(request, 'frontend/calculationorder.html', {'order': order})

@login_required
def calculation(request, pk):
    try:
        calc = Calculation.objects.get(pk=pk)
    except Calculation.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile
    if not can_view_calculation(calc, profile):
        return HttpResponse(status=404)

    return render(request, 'frontend/calculation.html', {'calc': calc})

@login_required
def see(request, pk):
    try:
        order = CalculationOrder.objects.get(pk=pk)
    except CalculationOrder.DoesNotExist:
        return HttpResponse(status=404)

    profile = request.user.profile
    if profile != order.author:
        return HttpResponse(status=404)

    order.see()

    return HttpResponse(status=200)

@login_required
def see_all(request):
    profile = request.user.profile

    calcs = CalculationOrder.objects.filter(author=profile, hidden=False)

    for c in calcs:
        if c.new_status:
            c.see()

    # This should be true if everything works.
    # If a glitch happens and the counter is off, this will reset it.
    profile.unseen_calculations = 0
    profile.save()

    return HttpResponse(status=200)

@login_required
def clean_all_successful(request):
    profile = request.user.profile

    to_update = []
    calcs = CalculationOrder.objects.filter(author=profile, hidden=False)
    for c in calcs:
        if c.status == 2:
            c.hidden = True
            c.see()
            to_update.append(c)

    CalculationOrder.objects.bulk_update(to_update, ['hidden', 'last_seen_status'])

    return HttpResponse(status=200)

@login_required
def clean_all_completed(request):
    profile = request.user.profile

    to_update = []
    calcs = CalculationOrder.objects.filter(author=profile, hidden=False)
    for c in calcs:
        if c.status in [2, 3]:
            c.hidden = True
            c.see()
            to_update.append(c)

    CalculationOrder.objects.bulk_update(to_update, ['hidden', 'last_seen_status'])

    return HttpResponse(status=200)

def change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, 'Your password was updated.')
            return HttpResponseRedirect("/change_password/")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'frontend/change_password.html', {
        'form': form
    })

'''
def handler404(request, *args, **argv):
    return render(request, 'error/404.html', {
            })

def handler403(request, *args, **argv):
    return render(request, 'error/403.html', {
            })

def handler400(request, *args, **argv):
    return render(request, 'error/400.html', {
            })

def handler500(request, *args, **argv):
    return render(request, 'error/500.html', {
            })
'''

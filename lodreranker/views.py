# lodreranker/views.py
import json

import jsonpickle
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.staticfiles import finders
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render, reverse
from django.urls import reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView
from django.views.generic.edit import CreateView, FormView, UpdateView
from social_django.models import UserSocialAuth

from lodreranker import constants, forms, utils
from lodreranker.models import (BeyondAccuracyMetric, CustomUser, RankerMetric,
                                RetrievedItem)
from lodreranker.recommendation import (GeoItemRetriever, ItemRanker,
                                        PoiItemRetriever, Recommender,
                                        SocialItemRetriever)


def home(request):
    return render(request, 'home.html')


# Wrapper for social:begin to conditionally skip user social login if already existing.
def social_login(request):
    is_skip = request.GET.get('skip', False)
    request.session['skip_creation'] = True if is_skip else False
    return redirect(reverse('social:begin', args=('facebook',)))


# Recap user profile data and social data
@login_required
def profile(request):
    if 'not_existing' in request.session.keys():
        request.session.pop('not_existing')

    user = request.user
    if not user.completed and all([
            user.has_social_data,
            user.has_demographic,
            user.has_movies,
            user.has_books,
            user.has_artists
        ]):
        user.completed = True
        user.save()

    try:
        facebook_login = user.social_auth.get(provider='facebook')
    except UserSocialAuth.DoesNotExist:
        facebook_login = None

    def get_info(mtype):
        return list(map(lambda x: f'{x.name}  ({x.wkd_id}, {len(x.abstract)} chars.)', user.social_items.filter(media_type=mtype)))

    can_disconnect = (user.social_auth.count() > 1 or user.has_usable_password())
    return render(request, 'profile.html', {
        'facebook_login': facebook_login,
        'can_disconnect': can_disconnect,
        f'{constants.MOVIE}': get_info(constants.MOVIE),
        f'{constants.BOOK}': get_info(constants.BOOK),
        f'{constants.MUSIC}': get_info(constants.MUSIC),
    })


# Handle Facebook disconnect and disassociate social auth from user
@login_required
def social_disconnect(request):
    user = request.user
    soc_auths = UserSocialAuth.objects.filter(user=user.id)
    if soc_auths:
        soc_auths[0].delete()
    user.has_social_connect = False
    user.has_social_data = False
    user.completed = False
    user.social_items.clear()
    user.save()
    return redirect(reverse_lazy('profile'))
    # return redirect(reverse_lazy('signup_s1'))


# Reset all user data
@login_required
def reset(request):
    user = request.user
    # user.poi_weights = None
    # user.has_poivector = False
    soc_auths = UserSocialAuth.objects.filter(user=user.id)
    if soc_auths:
        soc_auths[0].delete()
    user.form_movies = None
    user.form_books = None
    user.form_artists = None
    user.has_movies = False
    user.has_books = False
    user.has_artists = False
    for name in {f.name: None for f in user._meta.fields if f.null}:
        setattr(user, name, None)
    user.has_demographic = False
    user.completed = False
    user.save()
    return redirect(reverse_lazy('social_disconnect'))


# Helper view for signup pipeline
def route(request):
    user = request.user
    s1 = reverse_lazy('signup_s1')
    s2 = reverse_lazy('signup_s2')
    s3 = reverse_lazy('signup_s3')
    s4 = reverse_lazy('signup_s4')
    s5 = reverse_lazy('signup_s5')
    s6 = reverse_lazy('signup_s6')
    profile = reverse_lazy('profile')

    if not user.has_social_connect or not user.has_social_data:
        return redirect(s1)
    elif not user.has_demographic:
        return redirect(s2)
    # elif not user.has_poivector:
        # return redirect(s3)
    elif not user.has_movies:
        return redirect(s4)
    elif not user.has_books:
        return redirect(s5)
    elif not user.has_artists:
        return redirect(s6)
    else:
        return redirect(profile)

# Handles user creation, automatically logins the new user after submit.
class SignupS0View(CreateView):
    template_name = 'registration/signup.html'
    form_class = forms.CustomUserCreationForm

    def form_valid(self, form):
        form.save()
        username = self.request.POST['username']
        password = self.request.POST['password1']
        user = authenticate(username=username, password=password)
        login(self.request, user)
        return redirect(reverse_lazy('signup_s1'))


# Facebook connect, creates UserSocialAuth object.
@login_required
def signup_s1(request):
    template_name = 'registration/social_connect.html'
    user = request.user
    if user.has_social_connect and user.has_social_data:
        return route(request)
    else:
        session = request.session
        if 'retriever' in session.keys():
            session.pop('retriever')
        if 'mtypes' in session.keys():
            session.pop('mtypes')

        return render(request, template_name)

# Retrieve user media likes from UserSocialAuth.
@login_required
@csrf_exempt
def signup_s1_ajax(request):
    user = request.user
    social_auth = UserSocialAuth.objects.filter(user=user.id)[0]

    if request.is_ajax():
        session = request.session
        if 'mtypes' in session.keys():
            if request.POST.get('next_mtype'):
                session['mtypes'].pop(0)
                if not session['mtypes']:
                    session.pop('mtypes')
                    user.has_social_data = True
                    user.save()
                    return JsonResponse({'retrieval_done': True})
        else:
            session['mtypes'] = [ constants.MOVIE, constants.BOOK, constants.MUSIC ]

        if 'retriever' in session.keys():
            retriever = jsonpickle.decode(session['retriever'])
            retriever.retrieve_next()
        else:
            mediatype = session['mtypes'][0]
            retriever = SocialItemRetriever(mediatype)
            raw_data = {}
            if mediatype in social_auth.extra_data.keys():
                raw_data = social_auth.extra_data[mediatype]
            retriever.initialize(raw_data)

        encoded_retriever = jsonpickle.encode(retriever)
        session['retriever'] = encoded_retriever

        if not retriever.next:
            for itemid in retriever.retrieved_items:
                user.social_items.add(RetrievedItem.objects.get(wkd_id=itemid))
            exec(f'user.social_{retriever.mtype}_count = len(retriever.retrieved_items)')
            user.save()
            session.pop('retriever')

    return JsonResponse(json.loads(encoded_retriever))


# Demographic data form
class SignupS2View(LoginRequiredMixin, UpdateView):
    template_name = 'registration/demographic_form.html'
    form_class = forms.CustomUserDemographicDataForm

    def get_object(self, queryset=None):
        return get_object_or_404(CustomUser, pk=self.request.user.id)

    def form_valid(self, form):
        user = self.request.user
        user.gender = form.cleaned_data['gender']
        user.age = form.cleaned_data['age']
        user.profession = form.cleaned_data['profession']
        user.has_demographic = True
        user.save()
        return route(self.request)


# Cold-start form #1 (POIs)
@login_required
def signup_s3(request):
    """POI FORM IS DISABLED"""
    return redirect(reverse_lazy('profile'))
    # template_name = 'registration/imageform_places.html'
    # result = utils.handle_imgform(request, 'signup_s3', 'poi')
    # if result['success']:
    #     selected_images, poi_choices = result['data'][0], result['data'][1]
    #     vectors = utils.get_vectors_from_selection(selected_images, poi_choices)
    #     user = request.user
    #     user.has_poivector = True
    #     user.poi_weights = sum(vectors).tolist()
    #     user.save()
    #     return route(request)
    # else:
    #     return render(request, template_name, result['data'])


# Cold-start form #2 (Movies)
@login_required
def signup_s4(request):
    template_name = 'registration/imageform_media.html'
    result = utils.handle_imgform(request, 'signup_s4', constants.MOVIE)
    if result['success']:
        selected_images, movie_choices = result['data'][0], result['data'][1]
        user = request.user
        movies_vectors = utils.get_vectors_from_selection(selected_images, movie_choices)
        user.form_movies = json.dumps(list(map(lambda x: x.tolist(), movies_vectors)))
        user.has_movies = True
        user.save()
        return route(request)
    else:
        return render(request, template_name, result['data'])


# Cold-start form #3 (Books)
@login_required
def signup_s5(request):
    template_name = 'registration/imageform_media.html'
    result = utils.handle_imgform(request, 'signup_s5', constants.BOOK)
    if result['success']:
        selected_images, book_choices = result['data'][0], result['data'][1]
        user = request.user
        books_vectors = utils.get_vectors_from_selection(selected_images, book_choices)
        user.form_books = json.dumps(list(map(lambda x: x.tolist(), books_vectors)))
        user.has_books = True
        user.save()
        return route(request)
    else:
        return render(request, template_name, result['data'])


# Cold-start form #4 (Artists)
@login_required
def signup_s6(request):
    template_name = 'registration/imageform_media.html'
    result = utils.handle_imgform(request, 'signup_s6', constants.MUSIC)
    if result['success']:
        selected_images, artist_choices = result['data'][0], result['data'][1]
        user = request.user
        artists_vectors = utils.get_vectors_from_selection(selected_images, artist_choices)
        user.form_artists = json.dumps(list(map(lambda x: x.tolist(), artists_vectors)))
        user.has_artists = True
        user.save()
        return route(request)
    else:
        return render(request, template_name, result['data'])


@login_required
def recommendation_view(request):
    mode = eval(f"request.{request.method}.get('mode')")
    template_name = f'recommendation/{mode}_recommendation.html'

    session = request.session
    context = {}

    if mode == constants.MODE_GEO:
        context['GOOGLE_MAPS_KEY'] = settings.GOOGLE_MAPS_KEY

    if request.method == 'GET':
        if 'results' in session.keys():
            if constants.REQUIRE_EVALUATION and not 'restart' in request.GET.keys():
                return redirect(reverse_lazy('recommendation_results'))
            else:
                session.pop('results')
        else:
            if 'retriever' in session.keys():
                session.pop('retriever')
            if 'mode' in session.keys():
                session.pop('mode')
            if 'retriever_name' in session.keys():
                session.pop('retriever_name')

        if mode == constants.MODE_GEO:
            if 'area' in session.keys():
                session.pop('area')

    elif request.method == 'POST':
        context['ajax_begin']  = True

        if mode == constants.MODE_GEO:
            area = utils.GeoArea(
                request.POST.get('latitude'),
                request.POST.get('longitude'),
                request.POST.get('radius')
            )
            session['area'] = jsonpickle.encode(area)
            # context variables for freezing the map
            context['map_enabled'] = 0
            context['latitude'] = request.POST.get('latitude')
            context['longitude'] = request.POST.get('longitude')

    return render(request, template_name, context)


@login_required
@csrf_exempt
def recommendation_view_ajax(request):
    user = request.user

    if request.is_ajax():
        session = request.session

        if 'mode' not in session.keys() and 'mode' in request.POST.keys():
            session['mode'] = request.POST.get('mode')

        if 'mtypes' in session.keys():
            if request.POST.get('next_mtype'):
                session['mtypes'].pop(0)
                if not session['mtypes']:
                    session.pop('mtypes')
                    return JsonResponse({'retrieval_done': True})
        else:
            session['mtypes'] = [constants.MOVIE, constants.BOOK, constants.MUSIC]
            # session['mtypes'] = [constants.MOVIE]

        if 'retriever' in session.keys():
            retriever = jsonpickle.decode(session['retriever'])
            retriever.retrieve_next()
        else:
            mediatype = session['mtypes'][0]

            if session['mode'] == constants.MODE_GEO:
                retriever = GeoItemRetriever(mediatype, limit=30)
                if 'area' in session.keys():
                    area = jsonpickle.decode(session['area'])
                retriever.initialize(area)

            elif session['mode'] == constants.MODE_POI:
                retriever = PoiItemRetriever(mediatype, limit=100)
                retriever.initialize("Colosseo") # TODO HARDCODED

        encoded_retriever = jsonpickle.encode(retriever)
        session['retriever'] = encoded_retriever

        if not retriever.next:
            session.pop('retriever')
            session['retriever_name'] = type(retriever).__name__
            recommender = Recommender(retriever.mtype, user, retriever)

            mode = session['mode']
            if mode == constants.MODE_GEO:
                methods = constants.METHODS
            elif mode == constants.MODE_POI:
                methods = constants.METHODS[:2]

            mtype_results = {}
            try:
                for method in methods:
                    mtype_results[method] = recommender.recommend(method=method, strip=True)
            except utils.RetrievalError:
                # mtype_results will be empty
                pass

            if 'results' in session.keys():
                session['results'][retriever.mtype] = mtype_results
            else:
                session['results'] = {retriever.mtype: mtype_results}

    return JsonResponse(json.loads(encoded_retriever))


@login_required
def recommendation_results(request):
    template_name = 'recommendation/results.html'
    context = {}
    session = request.session

    if request.method == 'GET':
        if 'results' in session.keys():
            results = session['results']
        else:
            return redirect(reverse_lazy('recommendation'))

        items = {}
        for mtype, mtype_data in results.items():
            if mtype_data:
                itemids = list(set([el['id'] for ranking in mtype_data.values() for el in ranking]))
                try:
                    mtype_items = [RetrievedItem.objects.get(wkd_id=itemid) for itemid in itemids]
                    items.update( [(item.wkd_id, item.__dict__) for item in mtype_items] )
                except RetrievedItem.DoesNotExist:
                    return # it should never ever fire

        context['has_results'] = any([results[x] for x in results.keys()])
        context['results'] = results
        context['items'] = items
        context['mode'] = session['mode']

        beyondaccuracy_text = {
            'rating': 'multimedia content that <b>matched my interests</b>',
            'novelty': 'multimedia content that <b>I did not know before</b>',
            'serendipity': 'surprisingly interesting multimedia content that <b>I might not have known in other ways</b>',
            'diversity': 'multimedia content that <b>are different to each other</b> (among content of the same type)',
        }
        context['beyondaccuracy'] = beyondaccuracy_text

    elif request.method == 'POST':
        post_dict = request.POST

        mode = session['mode']
        if mode == constants.MODE_GEO:
            methods = constants.METHODS
        elif mode == constants.MODE_POI:
            methods = constants.METHODS[:2]

        ranker_values = [(key, post_dict[key]) for key in post_dict if key.startswith('ranking')]
        for ranking_str in ranker_values:
            ranking = [int(x) for x in ranking_str[1].split(',')]
            # score=2 if method is 1st position (0)
            # score=1 if method is 2st position (1)
            # score=0 if method is 3rd position (2)
            scores = [2 if x==0 else 0 if x==2 else x for x in ranking]
            scores_dict  = { method: scores[i] for i, method in enumerate(methods)}
            scores_dict.update({'retriever': session['retriever_name']})
            rankermetric = RankerMetric(**scores_dict)
            rankermetric.save()

        beyondaccuracy_values = [(key, post_dict[key]) for key in post_dict if key.startswith('beyondaccuracy')]
        beyondaccuracy_dict = { x[0].split('_')[1]: int(x[1]) for x in beyondaccuracy_values }
        beyondaccuracy_dict.update({'retriever': session['retriever_name']})
        beyondaccuracymetric = BeyondAccuracyMetric(**beyondaccuracy_dict)
        beyondaccuracymetric.save()

        context['evaluation_done'] = True
        if 'results' in session.keys():
            session.pop('results')

    return render(request, template_name, context)

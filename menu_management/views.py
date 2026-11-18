from django.http import HttpResponse


def health(request):
    """Confirm Django can handle HTTP requests after startup initialization."""
    return HttpResponse('ok', content_type='text/plain')

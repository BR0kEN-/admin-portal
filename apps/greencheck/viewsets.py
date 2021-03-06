import csv
import logging
from io import TextIOWrapper

import tld
import urllib
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import pagination, parsers, request, response, viewsets
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.generics import CreateAPIView
from rest_framework.permissions import AllowAny
from drf_yasg.utils import swagger_auto_schema  # noqa

# flake8 doesn't like rest_framework_csv. It's not clear why
from rest_framework_csv import renderers as drf_csv_rndr  # noqa

from .api.ip_range_viewset import IPRangeViewSet  # noqa
from .api.asn_viewset import ASNViewSet  # noqa

from .models import GreenDomain, Hostingprovider
from .serializers import (
    GreenDomainBatchSerializer,
    GreenDomainSerializer,
)
from .domain_check import GreenDomainChecker

logger = logging.getLogger(__name__)


class GreenDomainViewset(viewsets.ReadOnlyModelViewSet):
    """
    The greencheck service to replicate the older PHP API for checking domains.

    Supports the same single and batch API.

    By default serves a response
    from the GreenDomains table, rather than executing a full domain check.
    This gives fast, responses, but there is also the option of
    providing a slower, no-cache response that carries out the full domain lookup.
    """

    # swagger_schema = None

    queryset = GreenDomain.objects.all()
    serializer_class = GreenDomainSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [AllowAny]
    lookup_field = "url"

    checker = GreenDomainChecker()

    def list(self, request, *args, **kwargs):
        """
        Our override for bulk URL lookups, like an index/listing view
        """
        queryset = []
        urls = None

        get_url_params = self.request.query_params.getlist("urls")
        if get_url_params:
            urlstring, *_ = get_url_params
            urls = urlstring.split(",")

        # check for a payload. this takes precedence, to support large requests
        if self.request.data.get("urls"):
            urls = self.request.data.getlist("urls")

        if urls is not None:
            queryset = GreenDomain.objects.filter(url__in=urls)

        # import ipdb

        # ipdb.set_trace()

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return response.Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """
        Fetch entry matching the provided URL, like a 'detail' view
        """
        url = self.kwargs.get("url")
        is_valid_tld = tld.is_tld(url)
        domain = None

        # TODO turn this into a function
        # not a domain, try ip address:
        if is_valid_tld:
            res = tld.get_tld(url, fix_protocol=True, as_object=True)
            domain = res.parsed_url.netloc

        if not is_valid_tld:
            parsed_url = urllib.parse.urlparse(url)
            if not parsed_url.netloc:
                # add the //, so that our url reading code
                # parses it properly
                parsed_url = urllib.parse.urlparse(f"//{url}")
            domain = parsed_url.netloc

        instance = GreenDomain.objects.filter(url=domain).first()

        if not instance:
            instance = self.perform_full_lookup(domain)

            # log_the_check asynchronously
            # self.log_check_async(site_check)

        serializer = self.get_serializer(instance)
        return response.Response(serializer.data)

    def perform_full_lookup(self, domain):
        """
        Return a Green Domain object from doing a lookup.
        """
        res = self.checker.check_domain(domain)
        hosting_provider = Hostingprovider.objects.get(pk=res.hosting_provider_id)

        # return a domain result, but don't save it,
        # as persisting it is handled asynchronously
        # by another worker, and logged to both the greencheck
        # table and this 'cache' table
        return GreenDomain(
            url=res.url,
            hosted_by=hosting_provider.name,
            hosted_by_id=hosting_provider.id,
            hosted_by_website=hosting_provider.website,
            partner=hosting_provider.partner,
            modified=res.checked_at,
            green=res.green,
        )


class GreenDomainBatchView(CreateAPIView):
    """
    A batch API for checking domains in bulk, rather than individually.

    Upload a CSV file containing a list of domains, to get back the status of each domain.

    If you just want a list of green domains to check against, we publish a daily snapshot of all the green domains we have, for offline use and analysis, at https://datasets.thegreenwebfoundation.org
    """  # noqa

    queryset = GreenDomain.objects.all()
    serializer_class = GreenDomainBatchSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [AllowAny]
    pagination_class = pagination.PageNumberPagination
    parser_classes = [parsers.FormParser, parsers.MultiPartParser]
    renderer_classes = [drf_csv_rndr.CSVRenderer]

    def collect_urls(self, request: request.Request) -> list:
        """
        Accept a request object, parse any attached CSV file, and
        return a list of the valid domains in the file,
        """
        url_file = self.request.data.get("urls")
        # attachments are by default binary, so we need to
        # convert them to a format the CSV reader expects
        encoded_file = TextIOWrapper(url_file, encoding="utf-8")
        csv_file = csv.reader(encoded_file)

        urls_list = []

        for row in csv_file:
            if row is not None:
                url, *_ = row
                domain = tld.get_fld(url, fix_protocol=True)
                urls_list.append(domain)

        return urls_list

    def build_green_greylist(self, grey_list: list, green_list) -> list:
        """
        Create a list of green and grey domains, to serialise and deliver.
        """
        grey_domains = []

        for domain in grey_list:
            gp = GreenDomain(url=domain)
            gp.hosted_by = None
            gp.hosted_by_id = None
            gp.hosted_by_website = None
            gp.partner = None
            gp.modified = timezone.now()
            grey_domains.append(gp)

        evaluated_green_queryset = green_list[::1]

        return evaluated_green_queryset + grey_domains

    def grey_urls_only(self, urls_list, queryset) -> list:
        """
        Accept a list of domain names, and a queryset of checked green
        domain objects, and return a list of only the grey domains.
        """
        green_list = [domain_object.url for domain_object in queryset]

        return [url for url in urls_list if url not in green_list]

    def create(self, request, *args, **kwargs):
        """
        """

        urls_list = self.collect_urls(request)

        logger.debug(f"urls_list: {urls_list}")

        if urls_list:
            queryset = GreenDomain.objects.filter(url__in=urls_list)

        grey_list = self.grey_urls_only(urls_list, queryset)

        combined_batch_check_results = self.build_green_greylist(grey_list, queryset)

        serialized = GreenDomainSerializer(combined_batch_check_results, many=True)

        headers = self.get_success_headers(serialized.data)

        return response.Response(serialized.data, headers=headers)

    def finalize_response(self, request, response, *args, **kwargs):
        """
        Override the default, so if we see a filename requested, send the
        header. This tells the client to treat it ike a file to download,
        rather than trying to display it inline if using a browser.
        """
        filename = request.data.get("response_filename")
        if filename is not None:
            response["Content-Disposition"] = f"attachment; filename={filename}"

        return super().finalize_response(request, response, *args, **kwargs)

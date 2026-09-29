# The py-contoso-analytics runtime, as a custom container for an ACA session pool.
#
# The base image is Microsoft's code-interpreter image: it already serves the
# dynamic-sessions `/executions` API on port 6000, so the agent calls this
# pool exactly the way it calls a managed PythonLTS pool. Everything on top of
# it comes from runtime/py-contoso-analytics.json and nowhere else.
FROM mcr.microsoft.com/k8se/services/codeinterpreter:0.9.18-python3.12

USER root
ARG RUNTIME=py-contoso-analytics
ENV CONTOSO_RUNTIME=${RUNTIME}

# 1. The private library. Built into a wheel here, installed from a file URL by
#    the manifest - the same path any private package of your own would take.
COPY contoso_sdk /opt/contoso/src/contoso_sdk
RUN python -m pip wheel --no-cache-dir --no-deps -w /opt/contoso/wheels /opt/contoso/src/contoso_sdk

# 2. Resolve and freeze the runtime. Fails the build if any declared pin does
#    not resolve to exactly that version.
COPY runtime/${RUNTIME}.json /opt/contoso/runtime/${RUNTIME}.json
COPY install_runtime.py /opt/contoso/install_runtime.py
RUN python /opt/contoso/install_runtime.py /opt/contoso/runtime/${RUNTIME}.json

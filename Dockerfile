FROM python:3.7

ENV PYTHONUNBUFFERED 1

ENV CALCUS_SCR_HOME "/calcus/scr"
ENV CALCUS_RESULTS_HOME "/calcus/results"
ENV CALCUS_KEY_HOME "/calcus/keys"
ENV CALCUS_TEST_SCR_HOME "/calcus/frontend/tests/scr"
ENV CALCUS_TEST_RESULTS_HOME "/calcus/frontend/tests/results"
ENV CALCUS_TEST_KEY_HOME "/calcus/frontend/tests/keys"

ENV EBROOTORCA "/binaries/orca"
ENV GAUSS_EXEDIR "/binaries/g16"
ENV LD_LIBRARY_PATH=$LD_LIBRARY_PATH:"/binaries/orca"
ENV CALCUS_DOCKER "True"

ENV PATH=$PATH:"/binaries/xtb:/binaries/g16:/binaries/orca:/binaries/other:/binaries/openmpi"
ENV LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/binaries/orca:/usr/lib/openmpi/

ADD requirements.txt /calcus/requirements.txt

WORKDIR /calcus/

RUN pip install -r requirements.txt
RUN apt update && apt install openbabel sshpass -y

RUN adduser --disabled-password --gecos '' calcus  


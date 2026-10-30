FROM sawyer-quest-builder:mobile-1.37.0
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH=/usr/lib/jvm/java-17-openjdk-amd64/bin:/opt/android-sdk/build-tools/32.0.0:$PATH
RUN /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager 'platforms;android-32' 'build-tools;32.0.0'

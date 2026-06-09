FROM envoyproxy/envoy:v1.31-latest

COPY envoy.yaml /etc/envoy/envoy.yaml

# Envoy runs as non-root (uid 101) by default — compatible with OpenShift SCCs
USER 101

EXPOSE 8080 9901

CMD ["/usr/local/bin/envoy", "-c", "/etc/envoy/envoy.yaml", "--log-level", "info"]

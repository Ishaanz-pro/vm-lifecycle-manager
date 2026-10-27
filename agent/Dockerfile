# Go host agent: static binary on a distroless, non-root base (no shell, no package manager).
ARG GO_IMAGE=golang:1.22-bookworm

FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
ARG VERSION=0.1.0
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=${VERSION}" -o /out/hostagent ./cmd/hostagent

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/hostagent /hostagent
USER nonroot:nonroot
EXPOSE 9100
# distroless has no shell or curl; the agent's own binary cannot health-check itself here,
# so compose/k8s probe /healthz over HTTP instead.
ENTRYPOINT ["/hostagent"]

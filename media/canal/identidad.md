# Identidad de ClipsAfk

Todo lo que hay que pegar en YouTube Studio. Los archivos están en esta misma carpeta.

---

## Nombre

```
ClipsAfk
```

Se mantiene el que ya habías elegido. Es corto, se lee, "AFK" es nativo del mundo
gaming y no lleva acentos, que importa para el identificador. Renombrar más adelante
cuesta más que cualquier mejora marginal ahora.

## Identificador

```
@clipsafk
```

Si está ocupado: `@clipsafkes` o `@clipsafk_`. Evitar números sueltos al final, que
leen a cuenta falsa.

## Descripción

```
Los mejores momentos del streaming en español.

Clips de auronplay, ibai, rubius, elxokas y más — elegidos por lo que de verdad hizo
reír al chat, no por el título.

Dos clips nuevos cada día, a las 09:00 y las 18:00 (hora Colombia).

Todos los clips pertenecen a sus creadores originales, y cada video enlaza al canal y
al clip de origen. Si eres creador y quieres que retire alguno, escríbeme y lo hago.
```

Lo que hace trabajo ahí: **una promesa concreta y un horario**. Nueve mil vistas
dejaron tres suscriptores porque nada le decía a nadie qué gana quedándose.

## Palabras clave del canal

```
clips, twitch, kick, streamers, español, auronplay, ibai, rubius, elxokas, shorts,
humor, gaming, clips en español, momentos divertidos
```

---

## Archivos

| Archivo | Uso | Medidas |
|---|---|---|
| `avatar.png` | Foto de perfil | 800×800 |
| `banner.png` | Banner del canal | 2048×1152 |

El avatar es **AFK en grande sobre degradado azul-morado**. En el feed de Shorts se ve
a 32-48 píxeles: ahí sólo sobreviven pocas letras con mucho contraste. Cualquier
detalle fino desaparece y sólo ensucia.

El banner tiene todo el texto dentro de la zona segura central de 1235×338, que es lo
único visible en móvil.

---

## Qué se puede automatizar y qué no

| | Por API | Notas |
|---|---|---|
| Nombre, descripción, palabras clave | Sí | Requiere el scope `youtube`, ya añadido al código. Hay que reconectar la cuenta para que se conceda |
| Banner | Sí | `channelBanners.insert` |
| **Foto de perfil** | **No** | YouTube no lo expone en la API. Sólo desde Studio, con ningún permiso alcanza |

Como el avatar obliga a pasar por Studio de todos modos, lo práctico es hacer todo
allí de una vez: son dos minutos.

## Pasos en YouTube Studio

1. **Personalización → Marca**: subir `avatar.png` como foto y `banner.png` como banner
2. **Personalización → Información básica**: pegar nombre y descripción
3. **Configuración → Canal → Información básica**: pegar las palabras clave, país
   Colombia, idioma español
4. **Personalización → Diseño**: poner el video de mejor retención como destacado para
   visitantes nuevos

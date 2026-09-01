# 🤖 Sayri Gateway Plugin: Discord Bot

Este plugin permite conectar cualquier servidor de **Discord** o canales de mensajes directos (DMs) directamente con tus agentes de Inteligencia Artificial en **Sayri (Pulsar OS)**.

---

## 🌟 Características Principales

- **Invocación Flexible**:
  - En canales de servidores: `/sayri <mensaje>`, `!sayri <mensaje>` o mencionando al bot `@SayriBot <mensaje>`.
  - En Mensajes Directos (DMs): Escribe directamente cualquier duda y el bot te responderá.
- **Lectura y Resumen Inteligente del Canal**:
  - Si le dices a Sayri *"resume los últimos mensajes"*, *"qué han dicho arriba"* o *"haz un resumen del canal"*, el gateway consulta los últimos mensajes del canal mediante la API REST de Discord y le entrega el contexto completo al agente para su análisis.
- **Emparejamiento Seguro de Escritorio (OTP Pairing)**:
  - Sistema de protección contra accesos no autorizados con PIN de 6 dígitos generado en tu escritorio de Pulsar OS.
  - Protección contra fuerza bruta con límite de intentos y rotación automática del PIN.
- **Multi-Instancia y Sandboxing**:
  - Puedes crear múltiples instancias del bot de Discord conectadas a diferentes agentes (ej. *Sayri Principal*, *Asistente Programador*, etc.) con diferentes niveles de aislamiento (`LEVEL_0_NO_EXEC` hasta `LEVEL_3_HOST_USER`).
- **Cero Dependencias Externas**:
  - Implementado en Python puro con WebSockets RFC 6455 sobre TLS nativo y API REST v10.

---

## 📖 Guía Paso a Paso de Configuración

### Paso 1: Crear la Aplicación y Bot en Discord
1. Entra en el [Discord Developer Portal](https://discord.com/developers/applications).
2. Haz clic en el botón superior derecho **`New Application`** y ponle un nombre (ej. `Sayri Assistant`).
3. En el menú lateral izquierdo, entra en la pestaña **`Bot`**.
4. Haz clic en **`Reset Token`** (o *Copy*), copia el **Bot Token** y guárdalo (lo necesitarás en Sayri).

### Paso 2: Activar los Privileged Gateway Intents (¡Imprescindible!)
1. En la misma pestaña **`Bot`**, baja hasta la sección **`Privileged Gateway Intents`**.
2. **Activa obligatoriamente**:
   - ✅ **`MESSAGE CONTENT INTENT`** *(Requerido para que el bot pueda leer el texto de `/sayri <mensaje>` en los canales)*.
   - ✅ **`SERVER MEMBERS INTENT`** *(Recomendado para identificar a los usuarios del servidor)*.
3. Haz clic en **`Save Changes`** abajo.

### Paso 3: Generar la Invitación del Bot con los Permisos
1. En el menú lateral izquierdo, ve a **`OAuth2`** ➔ **`URL Generator`**.
2. En la casilla **`SCOPES`**, marca únicamente:
   - ✅ **`bot`**
3. Abajo aparecerá la sección **`BOT PERMISSIONS`**. Marca los siguientes permisos:
   - ✅ **`Send Messages`** *(Enviar mensajes)*
   - ✅ **`Send Messages in Threads`** *(Enviar mensajes en hilos)*
   - ✅ **`Read Message History`** *(Leer historial de mensajes, necesario para la función de resumen)*
   - ✅ **`View Channels`** *(Ver canales)*
   - ✅ **`Use External Emojis`** *(Opcional)*
4. Copia la **`GENERATED URL`** que aparece al final de la página.
5. Pégala en tu navegador y selecciona el servidor de Discord al que deseas invitar a tu bot.

---

## ⚙️ Configuración en Sayri (Pulsar OS)

1. Abre Sayri y pulsa en el botón de **Ajustes** ⚙️.
2. Ve a la pestaña **Gateways** y haz clic en **`+ Add Gateway`**.
3. En el formulario:
   - **Plataforma**: Selecciona `Discord Bot Gateway (sayri-gateway-discord)`.
   - **Nombre de la instancia**: Ej. `Discord - Servidor Principal`.
   - **Agente Vinculado**: Selecciona el agente (ej. `Sayri Principal`).
   - **Nivel de Sandbox**: Selecciona el nivel de seguridad (ej. `LEVEL_1_READONLY`).
   - **Bot Token**: Pega el token que copiaste en el Paso 1.
4. Haz clic en **`Create Gateway Instance`**. El bot se conectará inmediatamente.

---

## 🔑 Emparejar tu Cuenta de Discord

Por seguridad, Sayri rechaza mensajes de usuarios desconocidos hasta que se emparejan con el escritorio:

1. En Sayri, en la tarjeta de tu Gateway de Discord, pulsa en el botón **`🔑 Show Pairing PIN`** (mostrará un código de 6 dígitos).
2. En tu servidor de Discord o por mensaje directo con el bot, escribe:
   ```text
   /sayri /pair 123456
   ```
   *(Sustituyendo `123456` por el PIN mostrado en tu pantalla).*
3. El bot te responderá confirmando que tu usuario ha sido autorizado. ¡Ya puedes hablar con Sayri libremente!

---

## 💬 Comandos Disponibles en Discord

| Comando | Descripción |
| :--- | :--- |
| `/sayri <pregunta>` | Envía una consulta o petición a Sayri. |
| `!sayri <pregunta>` | Prefijo alternativo para consultar a Sayri. |
| `@SayriBot <pregunta>` | Mención directa al bot en cualquier canal. |
| `/sayri resume los últimos mensajes` | Lee los mensajes recientes del canal y genera un resumen estructurado. |
| `/sayri /pair <PIN>` | Empareja y autoriza tu cuenta de Discord con Sayri. |
